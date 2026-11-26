from time import timezone

from django.shortcuts import redirect
from django.views.generic import ListView, CreateView, UpdateView, DeleteView, DetailView
from django.urls import reverse_lazy
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth.mixins import LoginRequiredMixin
from workforce.models import Timesheet, EmployeeProfile
from .forms import TimesheetForm
from operations.models import Project
from django.views import View
from django.shortcuts import get_object_or_404
from django.contrib import messages
from django.core.exceptions import ValidationError as ModelValidationError
from workforce.services import generate_payroll
from workforce.models import PayrollRun, PayrollItem
from workforce.models import PayrollRun, PayrollItem, EmployeeProfile
from finance.models import Expense, ExpenseCategory, ExpenseStatus, Invoice, InvoiceItem
from operations.models import Project
from django.forms import ValidationError as FormValidationError



class MyTimesheetListView(LoginRequiredMixin, ListView):
    model = Timesheet
    template_name = "workforce/timesheet/my_list.html"
    context_object_name = "timesheets"

    def get_queryset(self):
        try:
            employee = EmployeeProfile.objects.get(user=self.request.user)
            return Timesheet.objects.filter(employee=employee).order_by("-date")
        except EmployeeProfile.DoesNotExist:
            return Timesheet.objects.none()



class TimesheetCreateView(LoginRequiredMixin, CreateView):
    model = Timesheet
    form_class = TimesheetForm
    template_name = "workforce/timesheet/form.html"
    success_url = reverse_lazy("my_timesheets")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.employee = self.request.user.employee_profile
        form.instance.status = "submitted"

        try:
           
            return super().form_valid(form)
        except ModelValidationError as e:
           
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


class TimesheetUpdateView(LoginRequiredMixin, UpdateView):
    model = Timesheet
    form_class = TimesheetForm
    template_name = "workforce/timesheet/form.html"
    success_url = reverse_lazy("my_timesheets")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()

       
        if obj.employee.user != request.user:
            raise PermissionDenied("Cannot edit another user's timesheet.")
        if obj.status != "submitted":
            raise PermissionDenied("Only submitted timesheets can be edited.")

        return super().dispatch(request, *args, **kwargs)


class TimesheetDeleteView(LoginRequiredMixin, DeleteView):
    model = Timesheet
    template_name = "workforce/timesheet/confirm_delete.html"
    success_url = reverse_lazy("my_timesheets")

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()

       
        if obj.employee.user != request.user:
            raise PermissionDenied("Cannot delete another user's timesheet.")
        if obj.status != "submitted":
            raise PermissionDenied("Only submitted timesheets can be deleted.")

        return super().dispatch(request, *args, **kwargs)
    

class ProjectTimesheetListView(LoginRequiredMixin, ListView):
    model = Timesheet
    template_name = "workforce/timesheet/project_list.html"
    context_object_name = "timesheets"

    def get_queryset(self):
       
        try:
            manager = EmployeeProfile.objects.get(user=self.request.user)
        except EmployeeProfile.DoesNotExist:
            return Timesheet.objects.none()

        managed_projects = Project.objects.filter(project_manager__user=self.request.user)
        return Timesheet.objects.filter(project__in=managed_projects).order_by("-date")
    
class TimesheetApproveView(LoginRequiredMixin, View):
    def post(self, request, pk, *args, **kwargs):
        timesheet = get_object_or_404(Timesheet, pk=pk)

     
        if timesheet.project.project_manager.user != request.user:
            raise PermissionDenied("You are not the manager of this project.")

       
        if timesheet.status != "submitted":
            raise PermissionDenied("Only submitted timesheets can be approved.")

        timesheet.status = "approved"
        timesheet.save()
        return redirect("project_timesheets")
    


def _can_manage_payroll(user):
    return user.is_authenticated and user.has_perm("workforce.add_payrollrun")


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


class PayrollManagerMixin(LoginRequiredMixin):
    """Only people who may run payroll (and only for their own company)."""
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not _can_manage_payroll(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


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
        return PayrollRun.objects.filter(company=self.request.user.company)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        items = self.object.items.select_related("employee__user").order_by("employee__user__first_name", "employee__user__email")
        context["items"] = items
        context["totals"] = _ytd(items)
        context["total_hours"] = sum(i.hours_worked for i in items)
        return context


class PayrollRunCreateView(LoginRequiredMixin, CreateView):
    model = PayrollRun
    fields = ["period_start", "period_end"]
    template_name = "payroll/payroll_run_form.html"

    def form_valid(self, form):
        form.instance.company = self.request.user.company
        form.instance.created_by = self.request.user
        try:
            response = super().form_valid(form)
            generate_payroll(form.instance)
            messages.success(self.request, "Payroll generated successfully!")
            return response
        except ValidationError as e:
          
            form.add_error(None, e) 
            return self.form_invalid(form)
        except Exception as e:
            messages.error(self.request, f"Payroll generation failed: {e}")
            return self.form_invalid(form)

    def get_success_url(self):
        return reverse_lazy("payroll_list")


    
class PayrollRunListView(PayrollManagerMixin, ListView):
    model = PayrollRun
    template_name = "payroll/payroll_list.html"
    context_object_name = "payroll_runs"

    def get_queryset(self):
        from django.db.models import Count, Sum
        return (PayrollRun.objects.filter(company=self.request.user.company)
                .select_related("created_by")
                .annotate(people=Count("items"), gross=Sum("items__gross_pay"),
                          deducted=Sum("items__deductions"), net=Sum("items__net_pay")))

