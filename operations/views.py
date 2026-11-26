from django.views.generic import DetailView, CreateView
from .forms import ProjectWithClientForm
from operations.models import Client
from django.shortcuts import get_object_or_404, redirect
from .models import Company, Project
from finance.models import Expense
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth
from .forms import EmployeeProjectForm

class CompanyProjectListView(DetailView):
    model = Company
    template_name = "operations/company_projects.html"
    context_object_name = "company"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["projects"] = self.object.projects.select_related(
            "project_manager", "client"
        ).prefetch_related("employee_assignments__employee")
        return context


# class ProjectDetailView(DetailView):
#     model = Project
#     template_name = "operations/project_detail.html"

#     context_object_name = "project"

#     def get_queryset(self):
#         return Project.objects.select_related(
#             "company", "project_manager", "client"
#         ).prefetch_related("employee_assignments__employee")

#     def get_context_data(self, **kwargs):
#         context = super().get_context_data(**kwargs)
#         project = self.object

       
#         total_approved_expenses = Expense.objects.filter(
#             project=project,
#             status="approved"  
#         ).aggregate(total=Sum('amount'))['total'] or 0

#         context['total_approved_expenses'] = total_approved_expenses

       
#         if project.budget and project.budget != 0:
#             profit_loss_percentage = ((project.budget - total_approved_expenses) / project.budget) * 100
#         else:
#             profit_loss_percentage = 0

#         context['profit_loss_percentage'] = round(profit_loss_percentage, 2)
#         return context





class ProjectDetailView(DetailView):
    model = Project
    template_name = "operations/project_detail.html"
    context_object_name = "project"

    def get_queryset(self):
        return Project.objects.select_related(
            "company", "project_manager", "client"
        ).prefetch_related("employee_assignments__employee")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        project = self.object

        # Expense info
        total_approved_expenses = Expense.objects.filter(
            project=project,
            status="approved"
        ).aggregate(total=Sum('amount'))['total'] or 0
        context['total_approved_expenses'] = total_approved_expenses

        if project.budget and project.budget != 0:
            profit_loss_percentage = ((project.budget - total_approved_expenses) / project.budget) * 100
        else:
            profit_loss_percentage = 0
        context['profit_loss_percentage'] = round(profit_loss_percentage, 2)

        # Hours logged on this project by each assigned person (one query)
        from workforce.models import Timesheet
        logged = {r["employee_id"]: r for r in Timesheet.objects.filter(project=project).values("employee_id").annotate(
            total=Sum("hours_worked"), approved=Sum("hours_worked", filter=Q(status="approved")))}
        context['assignments'] = [
            (a, (logged.get(a.employee_id) or {}).get("total") or 0, (logged.get(a.employee_id) or {}).get("approved") or 0)
            for a in project.employee_assignments.all()]
        context['total_hours'] = sum((row["total"] or 0) for row in logged.values())

        # EmployeeProject form
        context['employee_project_form'] = EmployeeProjectForm(company=project.company)
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = EmployeeProjectForm(request.POST, company=self.object.company)
        if form.is_valid():
            assignment = form.save(commit=False)
            assignment.project = self.object
            assignment.save()
            return redirect('project_detail', pk=self.object.pk)
        context = self.get_context_data()
        context['employee_project_form'] = form
        return self.render_to_response(context)    

class ProjectCreateView(CreateView):
    model = Project
    form_class = ProjectWithClientForm
    template_name = "operations/project_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user  # pass user to form
        return kwargs
    
    def get_context_data(self, **kwargs):
     context = super().get_context_data(**kwargs)
     context['company'] = self.request.user.employee_profile.company
     return context

    def form_valid(self, form):
        # Create the client first
        client = Client.objects.create(
            company=self.request.user.employee_profile.company,
            name=form.cleaned_data['client_name'],
            email=form.cleaned_data['client_email'],
            phone=form.cleaned_data['client_phone'],
            address=form.cleaned_data['client_address'],
        )

        # Now create the project
        project = form.save(commit=False)
        project.company = self.request.user.employee_profile.company
        project.client = client
        project.save()

        return redirect('company_projects', pk=project.company.id)


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


def _month_range(first, last):
    """Yield the first day of every month from `first` to `last` inclusive."""
    cur = date(first.year, first.month, 1)
    last = date(last.year, last.month, 1)
    while cur <= last:
        yield cur
        cur = date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)


class ProjectTimelineView(LoginRequiredMixin, DetailView):
    """Month-by-month view of one project: hours logged, invoiced, received and expenses."""
    model = Project
    template_name = "operations/project_timeline.html"
    context_object_name = "project"

    def get_queryset(self):
        return Project.objects.filter(company=self.request.user.company).select_related("client", "company")

    def get_context_data(self, **kwargs):
        from finance.models import Payment, ProjectPaymentInvoice
        from workforce.models import EmployeeProfile, Timesheet
        from operations.models import EmployeeProject

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
        tot = dict(hours=ZERO, approved=ZERO, invoiced=ZERO, received=ZERO, spent=ZERO, invoices=0)
        for m in months:
            h = sum((v[0] for (mm, _), v in hours.items() if mm == m), ZERO)
            ah = sum((v[1] for (mm, _), v in hours.items() if mm == m), ZERO)
            inv, n = invoiced.get(m, (ZERO, 0))
            rec, exp = received.get(m, ZERO), spent.get(m, ZERO)
            balance += inv - rec
            rows.append(dict(month=m, hours=h, approved=ah, invoiced=inv, invoices=n, received=rec,
                             outstanding=balance, spent=exp,
                             per_hour=(inv / h).quantize(Decimal("0.01")) if h else None))
            for k, v in (("hours", h), ("approved", ah), ("invoiced", inv), ("received", rec), ("spent", exp)):
                tot[k] += v
            tot["invoices"] += n

        people = []
        for pid, prof in profiles.items():
            per_month = [(hours.get((m, pid)) or [ZERO, ZERO])[0] for m in months]
            people.append(dict(
                name=prof.user.get_full_name() or prof.user.email, title=prof.job_title,
                role=assigned.get(pid, "Logged hours (not assigned)"), per_month=per_month,
                total=sum(per_month, ZERO)))
        people.sort(key=lambda p: -p["total"])

        context.update(
            rows=rows, people=people, totals=tot, outstanding=tot["invoiced"] - tot["received"],
            per_hour=(tot["invoiced"] / tot["hours"]).quantize(Decimal("0.01")) if tot["hours"] else None,
            months=months, started=months[0] if months else None,
            chart=dict(labels=[m.strftime("%b %Y") for m in months],
                       hours=[float(r["hours"]) for r in rows],
                       invoiced=[float(r["invoiced"]) for r in rows],
                       received=[float(r["received"]) for r in rows]),
        )
        return context
