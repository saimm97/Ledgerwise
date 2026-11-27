import logging

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from Ledgerwise.mixins import (
    FINANCE_MANAGER_PERM,
    SecureViewMixin,
    user_manages_project,
)
from workforce.models import EmployeeProfile, PayrollItem, PayrollRun, Timesheet
from workforce.services import generate_payroll

from .forms import TimesheetForm

logger = logging.getLogger(__name__)


class MyTimesheetListView(LoginRequiredMixin, ListView):
    model = Timesheet
    template_name = "workforce/timesheet/my_list.html"
    context_object_name = "timesheets"

    def get_queryset(self):
        return (
            Timesheet.objects.filter(employee__user=self.request.user)
            .select_related("project")
            .order_by("-date")
        )


class TimesheetCreateView(SecureViewMixin, CreateView):
    model = Timesheet
    form_class = TimesheetForm
    template_name = "workforce/timesheet/form.html"
    success_url = reverse_lazy("my_timesheets")
    require_profile = True

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.employee = self.profile
        form.instance.status = "submitted"

        try:
            return super().form_valid(form)
        except ValidationError as e:
            form.add_error(None, e)
            shown_errors = set()
            for msg in form.non_field_errors():
                if msg not in shown_errors:
                    messages.error(self.request, msg)
                    shown_errors.add(msg)
            return self.form_invalid(form)

    def form_invalid(self, form):
        shown_errors = set()
        for field in form:
            for error in field.errors:
                if error not in shown_errors:
                    messages.error(self.request, f"{field.label}: {error}")
                    shown_errors.add(error)
        return super().form_invalid(form)


class OwnSubmittedTimesheetMixin(LoginRequiredMixin):
    """Edit or delete only your own timesheets, and only while they are still "submitted".

    Anything else answers 404, so nobody can probe for other people's timesheet ids.
    """
    model = Timesheet
    success_url = reverse_lazy("my_timesheets")

    def get_queryset(self):
        return Timesheet.objects.filter(employee__user=self.request.user, status="submitted")


class TimesheetUpdateView(OwnSubmittedTimesheetMixin, UpdateView):
    form_class = TimesheetForm
    template_name = "workforce/timesheet/form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class TimesheetDeleteView(OwnSubmittedTimesheetMixin, DeleteView):
    template_name = "workforce/timesheet/confirm_delete.html"


class ProjectTimesheetListView(SecureViewMixin, ListView):
    """Timesheets on the projects the signed-in user manages."""
    model = Timesheet
    template_name = "workforce/timesheet/project_list.html"
    context_object_name = "timesheets"

    def get_queryset(self):
        return (
            Timesheet.objects.filter(
                project__company=self.request.user.company_id,
                project__project_manager__user=self.request.user,
            )
            .select_related("employee__user", "project")
            .order_by("-date")
        )


class TimesheetApproveView(SecureViewMixin, View):
    """Approve a submitted timesheet: the project's manager only, never your own, only once."""

    def post(self, request, pk, *args, **kwargs):
        timesheet = get_object_or_404(
            Timesheet.objects.select_related("project", "employee__user"),
            pk=pk,
            project__company=request.user.company_id,
        )
        if not user_manages_project(request.user, timesheet.project):
            raise PermissionDenied("You are not the manager of this project.")
        if timesheet.employee.user_id == request.user.id:
            raise PermissionDenied("You cannot approve your own timesheet.")

        with transaction.atomic():
            locked = Timesheet.objects.select_for_update().get(pk=timesheet.pk)
            if locked.status != "submitted":
                messages.error(request, "This timesheet has already been approved.")
                return redirect("project_timesheets")
            locked.status = "approved"
            locked.approved_by = request.user
            locked.approved_at = timezone.now()
            locked.save()

        messages.success(request, "Timesheet approved.")
        return redirect("project_timesheets")


def _can_manage_payroll(user):
    return user.is_authenticated and user.has_perm(FINANCE_MANAGER_PERM)


def _ytd(items):
    """Totals for a list or queryset of PayrollItem."""
    from django.db.models import Sum
    agg = items.aggregate(gross=Sum("gross_pay"), deductions=Sum("deductions"), net=Sum("net_pay"))
    return {k: (v or 0) for k, v in agg.items()}


class MyPayrollView(LoginRequiredMixin, ListView):
    """The signed-in person's payslip history, newest first, with year-to-date totals."""
    model = PayrollItem
    template_name = "payroll/my_payroll.html"
    context_object_name = "payroll_items"

    def get_queryset(self):
        profile = getattr(self.request.user, "employee_profile", None)
        if profile is None:
            # Users without an employee profile (for example a fresh superuser) have no payslips yet.
            return PayrollItem.objects.none()
        return (PayrollItem.objects.filter(employee=profile)
                .select_related("payroll_run").order_by("-payroll_run__period_start"))

    def get_context_data(self, **kwargs):
        from django.utils import timezone as dj_tz
        context = super().get_context_data(**kwargs)
        qs = self.object_list
        context["lifetime"] = _ytd(qs)
        context["this_year"] = _ytd(qs.filter(payroll_run__period_start__year=dj_tz.localdate().year))
        context["year"] = dj_tz.localdate().year
        return context


class PayrollManagerMixin(SecureViewMixin):
    """Only people who may run payroll, and only for their own company."""
    required_perm = FINANCE_MANAGER_PERM


def _payslip_context(item):
    """Everything a payslip page or PDF needs, taken from the stored snapshot plus the timesheets in the period."""
    from django.db.models import Sum
    run = item.payroll_run
    by_project = (Timesheet.objects.filter(employee=item.employee, date__gte=run.period_start, date__lte=run.period_end,
                                           status="approved")
                  .values("project__name").annotate(hours=Sum("hours_worked")).order_by("project__name"))
    earlier = PayrollItem.objects.filter(employee=item.employee, payroll_run__period_start__year=run.period_start.year,
                                         payroll_run__period_start__lte=run.period_start)
    return {"item": item, "run": run, "employee": item.employee, "person": item.employee.user,
            "company": run.company, "by_project": by_project, "ytd": _ytd(earlier)}


def _get_payslip(request, pk):
    item = get_object_or_404(PayrollItem.objects.select_related("payroll_run__company", "employee__user"), pk=pk)
    own = getattr(request.user, "employee_profile", None)
    is_own = own is not None and own.pk == item.employee_id
    is_manager = _can_manage_payroll(request.user) and request.user.company_id == item.payroll_run.company_id
    if not (is_own or is_manager):
        raise PermissionDenied
    return item


class PayslipView(LoginRequiredMixin, View):
    def get(self, request, pk):
        from django.shortcuts import render
        return render(request, "payroll/payslip.html", _payslip_context(_get_payslip(request, pk)))


class PayslipPDFView(LoginRequiredMixin, View):
    def get(self, request, pk):
        from django.http import HttpResponse
        from django.template.loader import render_to_string
        from weasyprint import HTML
        item = _get_payslip(request, pk)
        html = render_to_string("payroll/payslip_pdf.html", _payslip_context(item))
        response = HttpResponse(HTML(string=html).write_pdf(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="Payslip-{item.slip_no or item.pk}.pdf"'
        return response


class EmployeePayrollHistoryView(LoginRequiredMixin, ListView):
    """Every payslip ever issued to one employee (payroll managers, or the employee themself)."""
    template_name = "payroll/employee_history.html"
    context_object_name = "payroll_items"

    def get_employee(self):
        emp = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=self.kwargs["pk"])
        own = getattr(self.request.user, "employee_profile", None)
        if not ((own is not None and own.pk == emp.pk)
                or (_can_manage_payroll(self.request.user) and self.request.user.company_id == emp.company_id)):
            raise PermissionDenied
        return emp

    def get_queryset(self):
        self.employee = self.get_employee()
        return PayrollItem.objects.filter(employee=self.employee).select_related("payroll_run")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(employee=self.employee, lifetime=_ytd(self.object_list))
        return context


class PayrollRunDetailView(PayrollManagerMixin, DetailView):
    """One payroll run with every person's complete pay line."""
    model = PayrollRun
    template_name = "payroll/payroll_run_detail.html"
    context_object_name = "run"

    def get_queryset(self):
        return PayrollRun.objects.filter(company=self.request.user.company_id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        items = self.object.items.select_related("employee__user").order_by("employee__user__first_name", "employee__user__email")
        context["items"] = items
        context["totals"] = _ytd(items)
        context["total_hours"] = sum(i.hours_worked for i in items)
        return context


class PayrollRunCreateView(PayrollManagerMixin, CreateView):
    model = PayrollRun
    fields = ("period_start", "period_end")
    template_name = "payroll/payroll_run_form.html"

    def form_valid(self, form):
        form.instance.company = self.company
        form.instance.created_by = self.request.user
        try:
            # One transaction: a failure must not leave a half-made run behind.
            with transaction.atomic():
                response = super().form_valid(form)
                generate_payroll(form.instance)
        except ValidationError as e:
            form.add_error(None, e)
            return self.form_invalid(form)
        except Exception:
            logger.exception("Payroll generation failed for company %s", self.company.pk)
            messages.error(self.request, "Payroll could not be generated. Nothing was saved. Please try again or contact support.")
            return self.form_invalid(form)
        messages.success(self.request, "Payroll generated successfully!")
        return response

    def get_success_url(self):
        return reverse_lazy("payroll_list")


class PayrollRunListView(PayrollManagerMixin, ListView):
    model = PayrollRun
    template_name = "payroll/payroll_list.html"
    context_object_name = "payroll_runs"

    def get_queryset(self):
        from django.db.models import Count, Sum
        return (PayrollRun.objects.filter(company=self.request.user.company_id)
                .select_related("created_by")
                .annotate(people=Count("items"), gross=Sum("items__gross_pay"),
                          deducted=Sum("items__deductions"), net=Sum("items__net_pay")))

