from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, ListView, TemplateView, UpdateView
from weasyprint import HTML

from Ledgerwise.mixins import (
    FINANCE_MANAGER_PERM,
    SecureViewMixin,
    get_profile,
    secure_view,
)
from operations.models import Project

from .forms import (
    CompanyExpenseForm,
    ExpenseForm,
    PaymentForm,
    ProjectPaymentInvoiceForm,
)
from .models import (
    Expense,
    ExpenseStatus,
    Invoice,
    InvoiceItem,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    ProjectPaymentInvoice,
)
from .tasks import send_invoice_email, send_payment_invoice_email

# --------------------------------------------------------------------------- helpers

def _visible_expenses(user, queryset):
    """Finance managers see every expense of their company; everyone else sees only their own
    expenses and the ones on projects they manage."""
    if user.has_perm(FINANCE_MANAGER_PERM):
        return queryset
    profile = get_profile(user)
    if profile is None:
        return queryset.none()
    return queryset.filter(Q(employee=profile) | Q(project__project_manager=profile))


def _approvable_expenses(user, *, with_project):
    """Submitted expenses this user may decide on: never their own, and never another company's.

    Project expenses: the project's manager, or a finance manager.
    Company expenses (no project): finance managers only (the view enforces the permission).
    """
    queryset = (
        Expense.objects.filter(
            company=user.company_id,
            status=ExpenseStatus.SUBMITTED,
            project__isnull=not with_project,
        )
        .exclude(employee__user=user)
        .select_related("employee__user", "project", "category")
    )
    if with_project and not user.has_perm(FINANCE_MANAGER_PERM):
        profile = get_profile(user)
        return queryset.filter(project__project_manager=profile) if profile else queryset.none()
    return queryset


def _create_expense_invoice(expense, prefix, user, *, label, **extra):
    today = timezone.now().date()
    invoice = Invoice.objects.create(
        company=expense.company,
        expense=expense,
        invoice_no=f"{prefix}-{timezone.now().timestamp()}",
        issue_date=today,
        due_date=today + timedelta(days=5),
        subtotal=expense.amount,
        created_by=user,
        **extra,
    )
    InvoiceItem.objects.create(
        invoice=invoice,
        description=expense.description or label,
        category=expense.category.name if expense.category else "",
        quantity=1,
        unit_price=expense.amount,
    )
    return invoice


# --------------------------------------------------------------------------- expenses

class ExpenseCreateView(SecureViewMixin, CreateView):
    model = Expense
    form_class = ExpenseForm
    template_name = "finance/expense_form.html"
    success_url = reverse_lazy("expense-list")
    require_profile = True

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        expense = form.save(commit=False)
        expense.company = self.company
        expense.employee = self.profile
        expense.currency = self.company.currency
        expense.save()
        self.object = expense  # get_success_url() reads it
        messages.success(self.request, "Expense submitted successfully.")
        return HttpResponseRedirect(self.get_success_url())


class ExpenseListView(SecureViewMixin, ListView):
    model = Expense
    template_name = "finance/expense_list.html"
    context_object_name = "expenses"

    def get_queryset(self):
        queryset = Expense.objects.filter(company=self.request.user.company_id, project__isnull=False)
        return _visible_expenses(self.request.user, queryset).select_related("employee", "project", "category")


class ExpenseApprovalListView(SecureViewMixin, ListView):
    model = Expense
    template_name = "finance/expense_approval_list.html"
    context_object_name = "expenses"

    def get_queryset(self):
        return _approvable_expenses(self.request.user, with_project=True)


class ExpenseReviewMixin(SecureViewMixin):
    """Approve or reject one submitted expense, exactly once.

    The decision is made under a row lock and only while the expense is still "submitted",
    so a double click or a second reviewer cannot create a second invoice or flip a decision.
    """
    model = Expense
    fields = ("status",)
    with_project = True

    def get_queryset(self):
        return _approvable_expenses(self.request.user, with_project=self.with_project)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["status"].choices = [
            (ExpenseStatus.APPROVED.value, ExpenseStatus.APPROVED.label),
            (ExpenseStatus.REJECTED.value, ExpenseStatus.REJECTED.label),
        ]
        return form

    def on_approved(self, expense):
        """Hook: runs inside the transaction once an expense is approved."""

    def form_valid(self, form):
        with transaction.atomic():
            expense = (
                Expense.objects.select_for_update(of=("self",))
                .select_related("company", "project__client", "category")
                .get(pk=self.object.pk)
            )
            if expense.status != ExpenseStatus.SUBMITTED:
                messages.error(self.request, "This expense has already been reviewed.")
                return HttpResponseRedirect(self.get_success_url())
            expense.status = form.cleaned_data["status"]
            expense.approved_by = self.request.user
            expense.approved_at = timezone.now()
            expense.save()
            if expense.status == ExpenseStatus.APPROVED:
                self.on_approved(expense)
        messages.success(self.request, f"Expense {expense.status} successfully.")
        return HttpResponseRedirect(self.get_success_url())


class ExpenseApprovalUpdateView(ExpenseReviewMixin, UpdateView):
    template_name = "finance/expense_approval_update.html"
    success_url = reverse_lazy("expense_approval_list")

    def on_approved(self, expense):
        client = expense.project.client
        if not (client and client.email):
            return
        invoice = _create_expense_invoice(
            expense, "INV", self.request.user, label="Project Expense",
            client=client, project=expense.project, status=InvoiceStatus.SENT,
        )
        # Only queue the email once the invoice is safely committed.
        transaction.on_commit(lambda: send_invoice_email.delay(str(invoice.id)))


# --------------------------------------------------------------------------- company expenses

class CompanyExpenseCreateView(SecureViewMixin, CreateView):
    model = Expense
    form_class = CompanyExpenseForm
    template_name = "finance/company_expense_form.html"
    success_url = reverse_lazy("company-expense-list")
    require_profile = True

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        expense = form.save(commit=False)
        expense.company = self.company
        expense.employee = self.profile
        expense.currency = self.company.currency
        expense.save()
        self.object = expense  # get_success_url() reads it
        messages.success(self.request, "Company expense submitted successfully.")
        return HttpResponseRedirect(self.get_success_url())


class CompanyExpenseListView(SecureViewMixin, ListView):
    model = Expense
    template_name = "finance/company_expense_list.html"
    context_object_name = "expenses"

    def get_queryset(self):
        queryset = Expense.objects.filter(company=self.request.user.company_id, project__isnull=True)
        return _visible_expenses(self.request.user, queryset).select_related("employee", "category").order_by("-expense_date")


class CompanyExpenseApprovalListView(SecureViewMixin, ListView):
    model = Expense
    template_name = "finance/company_expense_approval_list.html"
    context_object_name = "expenses"
    required_perm = FINANCE_MANAGER_PERM

    def get_queryset(self):
        return _approvable_expenses(self.request.user, with_project=False).order_by("-expense_date")


class CompanyExpenseApprovalUpdateView(ExpenseReviewMixin, UpdateView):
    template_name = "finance/company_expense_approval_update.html"
    success_url = reverse_lazy("company-expense-approval-list")
    required_perm = FINANCE_MANAGER_PERM
    with_project = False

    def on_approved(self, expense):
        _create_expense_invoice(expense, "COMP", self.request.user, label="Company Expense", client=None, project=None)


class CompanyExpenseAccountantListView(SecureViewMixin, ListView):
    model = Expense
    template_name = "finance/company_expense_accountant_list.html"
    context_object_name = "expenses"
    required_perm = FINANCE_MANAGER_PERM

    def get_queryset(self):
        return (
            Expense.objects.filter(company=self.request.user.company_id, status=ExpenseStatus.APPROVED)
            .select_related("employee__user", "category", "project", "invoice")
            .order_by("-expense_date")
        )


class ExpenseInvoiceView(SecureViewMixin, View):
    required_perm = FINANCE_MANAGER_PERM

    def get(self, request, expense_id):
        expense = get_object_or_404(Expense, id=expense_id, company=request.user.company_id)
        invoice = Invoice.objects.filter(expense=expense).first()
        if not invoice:
            raise Http404("No invoice found for this expense.")
        html = render_to_string("finance/invoice_detail.html", {"expense": expense, "invoice": invoice})
        return HttpResponse(html)


class ExpenseInvoiceDownloadView(SecureViewMixin, View):
    required_perm = FINANCE_MANAGER_PERM

    def get(self, request, expense_id):
        expense = get_object_or_404(Expense, id=expense_id, company=request.user.company_id)
        invoice = getattr(expense, "invoice", None)
        if not invoice:
            raise Http404("No invoice found for this expense.")

        html_string = render_to_string("finance/invoice_detail_download.html", {"expense": expense, "invoice": invoice})
        response = HttpResponse(HTML(string=html_string).write_pdf(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="Invoice-{invoice.invoice_no}.pdf"'
        return response


# --------------------------------------------------------------------------- reports

class ReportView(SecureViewMixin, TemplateView):
    """Company-wide figures: finance managers only, and only their own company's data.

    Revenue is money actually received (confirmed client payments); expenses and project
    spend count only approved expenses.
    """
    template_name = "finance/report.html"
    required_perm = FINANCE_MANAGER_PERM

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = self.company
        today = timezone.localdate()
        client_invoices = ProjectPaymentInvoice.objects.filter(company=company)
        approved = Expense.objects.filter(company=company, status=ExpenseStatus.APPROVED)

        projects_budget = []
        for project in Project.objects.filter(company=company).annotate(
            spent=Sum("expenses__amount", filter=Q(expenses__status=ExpenseStatus.APPROVED))
        ):
            spent = project.spent or 0
            budget = project.budget or 0
            projects_budget.append({
                "name": project.name,
                "spent": spent,
                "budget": budget,
                "spent_pct": int((spent / budget) * 100) if budget > 0 else 0,
            })

        paid_ids = [inv.pk for inv in client_invoices if inv.balance_due <= 0 and inv.total_amount > 0]
        overdue_ids = [inv.pk for inv in client_invoices if inv.balance_due > 0 and inv.due_date < today]

        def display_status(inv):
            if inv.pk in paid_ids:
                return "Paid"
            if inv.pk in overdue_ids:
                return "Overdue"
            return "Partial" if inv.amount_paid > 0 else "Sent"

        context.update(
            total_revenue=Payment.objects.filter(
                company=company, status=PaymentStatus.CONFIRMED
            ).aggregate(total=Sum("amount"))["total"] or 0,
            total_expenses=approved.aggregate(total=Sum("amount"))["total"] or 0,
            paid_invoices_count=len(paid_ids),
            overdue_count=len(overdue_ids),
            projects_budget=projects_budget,
            recent_invoices=[
                {
                    "number": inv.invoice_no,
                    "client_name": inv.project.client.name if inv.project.client else "-",
                    "amount": inv.total_amount,
                    "status": display_status(inv),
                    "issue_date": inv.issue_date,
                }
                for inv in client_invoices.select_related("project__client").order_by("-issue_date")[:5]
            ],
        )
        return context


report_view = ReportView.as_view()


# --------------------------------------------------------------------------- invoices per company

def _own_company_or_404(request, company_id):
    """The URL carries a company id; it must be the signed-in user's own company."""
    if company_id != request.user.company_id:
        raise Http404("Company not found.")
    return request.user.company


@secure_view(perm=FINANCE_MANAGER_PERM)
def company_invoices_view(request, company_id):
    company = _own_company_or_404(request, company_id)
    project_id = request.GET.get("project")
    invoices = Invoice.objects.filter(company=company)
    if project_id:
        invoices = invoices.filter(project_id=project_id)
    context = {
        "company": company,
        "projects": company.projects.all(),
        "invoices": invoices,
        "selected_project_id": project_id or "",
    }
    return render(request, "finance/project_invoices.html", context)


@secure_view(perm=FINANCE_MANAGER_PERM)
def select_project_invoices(request, company_id):
    company = _own_company_or_404(request, company_id)
    return render(request, "finance/select_project.html", {"company": company, "projects": company.projects.all()})


# --------------------------------------------------------------------------- client billing

class ProjectPaymentInvoiceListView(SecureViewMixin, ListView):
    model = ProjectPaymentInvoice
    template_name = "finance/project_payment_invoice_list.html"
    context_object_name = "invoices"
    required_perm = FINANCE_MANAGER_PERM

    def get_queryset(self):
        return (
            ProjectPaymentInvoice.objects.filter(company=self.request.user.company_id)
            .select_related("project")
            .prefetch_related("payments")
        )


class ProjectPaymentInvoiceCreateView(SecureViewMixin, CreateView):
    model = ProjectPaymentInvoice
    form_class = ProjectPaymentInvoiceForm
    template_name = "finance/project_payment_invoice_form.html"
    required_perm = FINANCE_MANAGER_PERM

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        invoice = form.save(commit=False)
        invoice.company = self.company
        invoice.created_by = self.request.user
        invoice.invoice_no = f"INV-{timezone.now().timestamp()}"
        invoice.issue_date = timezone.now().date()
        invoice.save()
        transaction.on_commit(lambda: send_payment_invoice_email.delay(str(invoice.id)))
        messages.success(self.request, "Project Payment Invoice created and emailed.")
        return redirect("project-payment-invoice-list")


class PaymentCreateView(SecureViewMixin, View):
    template_name = "finance/payment_form.html"
    required_perm = FINANCE_MANAGER_PERM

    def get_invoice(self, pk):
        return get_object_or_404(ProjectPaymentInvoice, id=pk, company=self.request.user.company_id)

    def get(self, request, pk):
        invoice = self.get_invoice(pk)
        return render(request, self.template_name, {"form": PaymentForm(invoice=invoice), "invoice": invoice})

    def post(self, request, pk):
        invoice = self.get_invoice(pk)
        form = PaymentForm(request.POST, invoice=invoice)
        form.instance.invoice = invoice
        form.instance.company = request.user.company

        if form.is_valid():
            payment = form.save(commit=False)
            payment.created_by = request.user
            payment.received_by = request.user
            if payment.status == PaymentStatus.CONFIRMED and not payment.paid_at:
                payment.paid_at = timezone.now()
            payment.save()
            messages.success(request, "Payment recorded successfully.")
            return redirect("project-payment-invoice-list")

        return render(request, self.template_name, {"form": form, "invoice": invoice})


@secure_view(perm=FINANCE_MANAGER_PERM)
def project_invoice_payments(request, invoice_id):
    invoice = get_object_or_404(ProjectPaymentInvoice, id=invoice_id, company=request.user.company_id)
    payments = invoice.payments.all().order_by("-paid_at")
    return render(request, "finance/project_invoice_payments.html", {"invoice": invoice, "payments": payments})
