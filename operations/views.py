from datetime import date, datetime
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.shortcuts import redirect
from django.views.generic import CreateView, DetailView

from finance.models import Expense
from Ledgerwise.mixins import (
    SecureViewMixin,
    user_manages_project,
    user_sees_project_finances,
)

from .forms import EmployeeProjectForm, ProjectWithClientForm
from .models import Client, Company, EmployeeProject, Project


class CompanyProjectListView(SecureViewMixin, DetailView):
    """The signed-in user's own company only; another company's id answers 404."""
    model = Company
    template_name = "operations/company_projects.html"
    context_object_name = "company"

    def get_queryset(self):
        return Company.objects.filter(pk=self.request.user.company_id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        projects = list(
            self.object.projects.select_related("project_manager__user", "client")
            .prefetch_related("employee_assignments__employee")
        )
        context["projects"] = projects
        # Projects whose invoices and payments this user may open (drives the "Monthly view" link).
        context["finance_project_ids"] = {p.id for p in projects if user_sees_project_finances(user, p)}
        return context


class ProjectDetailView(SecureViewMixin, DetailView):
    model = Project
    template_name = "operations/project_detail.html"
    context_object_name = "project"

    def get_queryset(self):
        return Project.objects.filter(company=self.request.user.company_id).select_related(
            "company", "project_manager__user", "client"
        )

    def get_context_data(self, **kwargs):
        from workforce.models import Timesheet

        context = super().get_context_data(**kwargs)
        project = self.object
        user = self.request.user

        total_approved_expenses = (
            Expense.objects.filter(project=project, status="approved").aggregate(total=Sum("amount"))["total"] or 0
        )
        context["total_approved_expenses"] = total_approved_expenses
        if project.budget:
            context["profit_loss_percentage"] = round(
                ((project.budget - total_approved_expenses) / project.budget) * 100, 2
            )
        else:
            context["profit_loss_percentage"] = 0

        # Hours logged on this project by each assigned person (one query)
        logged = {
            row["employee_id"]: row
            for row in Timesheet.objects.filter(project=project).values("employee_id").annotate(
                total=Sum("hours_worked"), approved=Sum("hours_worked", filter=Q(status="approved"))
            )
        }
        context["assignments"] = [
            (a, (logged.get(a.employee_id) or {}).get("total") or 0, (logged.get(a.employee_id) or {}).get("approved") or 0)
            for a in project.employee_assignments.select_related("employee__user")
        ]
        context["total_hours"] = sum((row["total"] or 0) for row in logged.values())

        context["can_manage"] = user_manages_project(user, project)
        context["can_see_finances"] = user_sees_project_finances(user, project)
        if context["can_manage"]:
            context.setdefault("employee_project_form", EmployeeProjectForm(company=project.company))
        return context

    def post(self, request, *args, **kwargs):
        """Assign an employee. Only the project's manager (or someone allowed to change projects) may."""
        self.object = self.get_object()
        if not user_manages_project(request.user, self.object):
            raise PermissionDenied("Only the project manager can assign employees.")

        form = EmployeeProjectForm(request.POST, company=self.object.company)
        if not form.is_valid():
            context = self.get_context_data()
            context["employee_project_form"] = form
            return self.render_to_response(context)

        _, created = EmployeeProject.objects.get_or_create(
            project=self.object,
            employee=form.cleaned_data["employee"],
            defaults={"role_in_project": form.cleaned_data["role_in_project"]},
        )
        if created:
            messages.success(request, "Employee assigned to the project.")
        else:
            messages.info(request, "That employee is already assigned to this project.")
        return redirect("project_detail", pk=self.object.pk)


class ProjectCreateView(SecureViewMixin, CreateView):
    model = Project
    form_class = ProjectWithClientForm
    template_name = "operations/project_form.html"
    required_perm = "operations.add_project"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["company"] = self.company
        return context

    def form_valid(self, form):
        with transaction.atomic():
            client = Client.objects.create(
                company=self.company,
                name=form.cleaned_data["client_name"],
                email=form.cleaned_data["client_email"],
                phone=form.cleaned_data["client_phone"],
                address=form.cleaned_data["client_address"],
            )
            project = form.save(commit=False)
            project.company = self.company
            project.client = client
            project.save()
        return redirect("company_projects", pk=project.company_id)


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def _month_range(first, last):
    """Yield the first day of every month from `first` to `last` inclusive."""
    cur = date(first.year, first.month, 1)
    last = date(last.year, last.month, 1)
    while cur <= last:
        yield cur
        cur = date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)


class ProjectTimelineView(SecureViewMixin, DetailView):
    """Month-by-month view of one project: hours logged, invoiced, received and expenses."""
    model = Project
    template_name = "operations/project_timeline.html"
    context_object_name = "project"

    def get_queryset(self):
        return Project.objects.filter(company=self.request.user.company_id).select_related("client", "company")

    def get_object(self, queryset=None):
        project = super().get_object(queryset)
        # Invoices and payments are financial data: managers and the finance team only.
        if not user_sees_project_finances(self.request.user, project):
            raise PermissionDenied("Project finances are limited to the project manager and finance team.")
        return project

    def get_context_data(self, **kwargs):
        from finance.models import Payment, ProjectPaymentInvoice
        from operations.models import EmployeeProject
        from workforce.models import EmployeeProfile, Timesheet

        context = super().get_context_data(**kwargs)
        project = self.object
        ZERO = Decimal("0.00")

        # ---- one grouped query per source, never a query per month ----
        hours = {}      # (month, employee_id) -> [all hours, approved hours]
        for r in (Timesheet.objects.filter(project=project)
                  .annotate(m=TruncMonth("date")).values("m", "employee_id")
                  .annotate(h=Sum("hours_worked"), ah=Sum("hours_worked", filter=Q(status="approved")))):
            hours[(_as_date(r["m"]), r["employee_id"])] = [r["h"] or ZERO, r["ah"] or ZERO]

        invoiced = {_as_date(r["m"]): (r["total"] or ZERO, r["n"]) for r in (
            ProjectPaymentInvoice.objects.filter(project=project).annotate(m=TruncMonth("issue_date"))
            .values("m").annotate(total=Sum("total_amount"), n=Count("id")))}

        received = {_as_date(r["m"]): r["total"] or ZERO for r in (
            Payment.objects.filter(invoice__project=project, status="confirmed")
            .annotate(m=TruncMonth(Coalesce("paid_at", "created_at"))).values("m").annotate(total=Sum("amount")))}

        spent = {_as_date(r["m"]): r["total"] or ZERO for r in (
            Expense.objects.filter(project=project, status="approved").annotate(m=TruncMonth("expense_date"))
            .values("m").annotate(total=Sum("amount")))}

        # ---- month range: project start (or first activity) up to now / project end ----
        seen = [m for m, _ in hours] + list(invoiced) + list(received) + list(spent)
        today = date.today()
        first = project.start_date or (min(seen) if seen else today)
        last_wanted = min(today, project.end_date) if project.end_date else today
        last = max([last_wanted] + seen)
        months = list(_month_range(min([first] + seen), last))

        # ---- people: everyone assigned plus anyone who logged hours ----
        assigned = {a.employee_id: a.role_in_project for a in EmployeeProject.objects.filter(project=project)}
        ids = set(assigned) | {e for _, e in hours}
        profiles = {p.id: p for p in EmployeeProfile.objects.filter(id__in=ids).select_related("user")}

        rows = []
        balance = ZERO   # running amount still owed by the client at the end of each month
        tot = {"hours": ZERO, "approved": ZERO, "invoiced": ZERO, "received": ZERO, "spent": ZERO, "invoices": 0}
        for m in months:
            h = sum((v[0] for (mm, _), v in hours.items() if mm == m), ZERO)
            ah = sum((v[1] for (mm, _), v in hours.items() if mm == m), ZERO)
            inv, n = invoiced.get(m, (ZERO, 0))
            rec, exp = received.get(m, ZERO), spent.get(m, ZERO)
            balance += inv - rec
            rows.append({"month": m, "hours": h, "approved": ah, "invoiced": inv, "invoices": n, "received": rec,
                             "outstanding": balance, "spent": exp,
                             "per_hour": (inv / h).quantize(Decimal("0.01")) if h else None})
            for k, v in (("hours", h), ("approved", ah), ("invoiced", inv), ("received", rec), ("spent", exp)):
                tot[k] += v
            tot["invoices"] += n

        people = []
        for pid, prof in profiles.items():
            per_month = [(hours.get((m, pid)) or [ZERO, ZERO])[0] for m in months]
            people.append({
                "name": prof.user.get_full_name() or prof.user.email, "title": prof.job_title,
                "role": assigned.get(pid, "Logged hours (not assigned)"), "per_month": per_month,
                "total": sum(per_month, ZERO)})
        people.sort(key=lambda p: -p["total"])

        context.update(
            rows=rows, people=people, totals=tot, outstanding=tot["invoiced"] - tot["received"],
            per_hour=(tot["invoiced"] / tot["hours"]).quantize(Decimal("0.01")) if tot["hours"] else None,
            months=months, started=months[0] if months else None,
            chart={"labels": [m.strftime("%b %Y") for m in months],
                       "hours": [float(r["hours"]) for r in rows],
                       "invoiced": [float(r["invoiced"]) for r in rows],
                       "received": [float(r["received"]) for r in rows]},
        )
        return context
