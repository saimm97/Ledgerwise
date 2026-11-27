from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from workforce.models import (
    PayrollRun,
    PayrollItem,
    Timesheet,
    EmployeeProfile,
    TimesheetStatus,
    SalaryType,
    TimesheetPaymentStatus,
    PayrollRunStatus,
)
import datetime
from django.utils import timezone


def _working_days(start, end):
    """Monday to Friday days between two dates, both included."""
    return sum(
        1 for i in range((end - start).days + 1)
        if (start + datetime.timedelta(days=i)).weekday() < 5
    )


def _last_day_of_month(day):
    first_of_next = (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return first_of_next - datetime.timedelta(days=1)


def generate_payroll(payroll_run: PayrollRun):
    employees = EmployeeProfile.objects.filter(
        company=payroll_run.company,
        is_active=True
    )

    num_working_days = sum(
        1
        for i in range((payroll_run.period_end - payroll_run.period_start).days + 1)
        if (payroll_run.period_start + datetime.timedelta(days=i)).weekday() < 5
    )

    expected_hours = Decimal(num_working_days * 8)

    with transaction.atomic():
        for employee in employees:

          
            total_hours = (
                Timesheet.objects.filter(
                    employee=employee,
                    status=TimesheetStatus.APPROVED,
                    payed_status=TimesheetPaymentStatus.UNPAID,
                    date__gte=payroll_run.period_start,
                    date__lte=payroll_run.period_end,
                ).aggregate(total=Sum("hours_worked"))["total"]
                or Decimal("0.00")
            )

            total_hours = total_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            if total_hours == 0:
                continue 

           
            if employee.salary_type == SalaryType.HOURLY:
                hourly_rate = employee.hourly_rate or Decimal("0.00")
                gross_pay = total_hours * hourly_rate
            else:
                base_salary = employee.base_salary or Decimal("0.00")
                # A month's salary is spread over that month's working days. A period that
                # crosses two months takes the right share of each month.
                period_salary_cap = Decimal("0.00")
                month_start = payroll_run.period_start.replace(day=1)
                while month_start <= payroll_run.period_end:
                    month_end = _last_day_of_month(month_start)
                    month_days = _working_days(month_start, month_end)
                    in_period = _working_days(
                        max(month_start, payroll_run.period_start), min(month_end, payroll_run.period_end)
                    )
                    if month_days:
                        period_salary_cap += base_salary * Decimal(in_period) / Decimal(month_days)
                    month_start = month_end + datetime.timedelta(days=1)

                if expected_hours == 0:
                    gross_pay = Decimal("0.00")
                elif total_hours >= expected_hours:
                    gross_pay = period_salary_cap
                else:
                    work_ratio = total_hours / expected_hours
                    gross_pay = period_salary_cap * work_ratio

            gross_pay = gross_pay.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # Create PayrollItem
            PayrollItem.objects.update_or_create(
                payroll_run=payroll_run,
                employee=employee,
                defaults={
                    "gross_pay": gross_pay,
                    "deductions": Decimal("0.00"),
                    "net_pay": gross_pay,
                    "job_title": employee.job_title,
                    "salary_type": employee.salary_type,
                    "base_salary": employee.base_salary,
                    "hourly_rate": employee.hourly_rate,
                    "hours_worked": total_hours,
                    "expected_hours": expected_hours,
                    "working_days": num_working_days,
                },
            )

            
            Timesheet.objects.filter(
                employee=employee,
                status=TimesheetStatus.APPROVED,
                payed_status=TimesheetPaymentStatus.UNPAID,
                date__gte=payroll_run.period_start,
                date__lte=payroll_run.period_end,
            ).update(payed_status=TimesheetPaymentStatus.PAID)

       
        if not PayrollItem.objects.filter(payroll_run=payroll_run).exists():
            raise ValidationError("No approved, unpaid timesheets were found in this period, so there is nothing to pay.")
        payroll_run.status = PayrollRunStatus.COMPLETED
        payroll_run.processed_at = timezone.now()
        payroll_run.save()

