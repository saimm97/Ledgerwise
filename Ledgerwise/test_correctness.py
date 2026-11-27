"""Regression tests for wrong results and crashes found in the Phase B audit."""
import datetime
from decimal import Decimal
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from finance.models import Expense, ExpenseCategory, ExpenseStatus, Invoice, Payment, ProjectPaymentInvoice
from finance.tasks import send_payment_invoice_email
from Ledgerwise.tests import SecurityTestCase
from operations.models import Project
from workforce.models import PayrollRun, Timesheet
from workforce.services import _working_days, generate_payroll

TODAY = datetime.date(2025, 6, 2)  # a Monday, safely in the past


def expense_form(project, category, **extra):
    data = {"project": project.pk, "category": category.pk, "amount": "20.00", "description": "x", "expense_date": "2025-01-02"}
    data.update(extra)
    return data


class ExpenseChecksTests(SecurityTestCase):
    def test_expense_gets_company_currency(self):
        self.login(self.eng_a)
        self.client.post(reverse("expense-add"), expense_form(self.project, self.category, description="Lunch"))
        self.assertEqual(Expense.objects.get(description="Lunch").currency, self.company_a.currency)

    def test_negative_zero_and_future_amounts_rejected(self):
        self.login(self.eng_a)
        future = (timezone.localdate() + datetime.timedelta(days=3)).isoformat()
        for override in ({"amount": "-5"}, {"amount": "0"}, {"expense_date": future}):
            response = self.client.post(reverse("expense-add"), expense_form(self.project, self.category, **override))
            self.assertEqual(response.status_code, 200, override)
        self.assertEqual(Expense.objects.count(), 0)

    def test_other_company_category_not_offered_or_accepted(self):
        foreign = ExpenseCategory.objects.create(company=self.company_b, name="Beta secret")
        self.login(self.eng_a)
        page = self.client.get(reverse("expense-add"))
        self.assertNotContains(page, "Beta secret")
        response = self.client.post(reverse("expense-add"), expense_form(self.project, foreign))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Expense.objects.count(), 0)


class PaymentChecksTests(SecurityTestCase):
    def setUp(self):
        super().setUp()
        self.invoice = ProjectPaymentInvoice.objects.create(
            company=self.company_a, project=self.project, amount=Decimal("100"), due_date=datetime.date(2030, 1, 1), created_by=self.fin_a,
        )

    def pay(self, amount, status="confirmed"):
        self.login(self.fin_a)
        return self.client.post(reverse("project-payment-add", args=[self.invoice.pk]), {
            "amount": amount, "payment_method": "cash", "status": status, "transaction_ref": "t", "paid_at": "2025-01-01T10:00",
        })

    def test_valid_payment_saved(self):
        self.assertEqual(self.pay("40").status_code, 302)
        self.assertEqual(Payment.objects.count(), 1)

    def test_negative_overpaying_and_pending_overpaying_rejected(self):
        for amount, status in (("-5", "confirmed"), ("500", "confirmed"), ("500", "pending")):
            self.assertEqual(self.pay(amount, status).status_code, 200, (amount, status))
        self.assertEqual(Payment.objects.count(), 0)

    def test_payment_invoice_without_due_date_is_a_form_error_not_a_crash(self):
        self.login(self.fin_a)
        response = self.client.post(reverse("project-payment-invoice-add"), {"project": self.project.pk, "amount": "10"})
        self.assertEqual(response.status_code, 200)

    def test_email_skipped_when_project_has_no_client(self):
        self.project.client = None
        self.project.save()
        self.assertTrue(send_payment_invoice_email(str(self.invoice.pk)).startswith("Skipped"))


class ReportAndInvoicePageTests(SecurityTestCase):
    def test_report_uses_received_money_and_approved_expenses_only(self):
        invoice = ProjectPaymentInvoice.objects.create(
            company=self.company_a, project=self.project, amount=Decimal("200"), due_date=datetime.date(2030, 1, 1), created_by=self.fin_a,
        )
        Payment.objects.create(company=self.company_a, invoice=invoice, amount=Decimal("75"), status="confirmed")
        Payment.objects.create(company=self.company_a, invoice=invoice, amount=Decimal("50"), status="pending")
        self.make_expense(status=ExpenseStatus.APPROVED)
        self.make_expense(status=ExpenseStatus.SUBMITTED)
        self.make_expense(status=ExpenseStatus.REJECTED)
        self.login(self.fin_a)
        context = self.client.get(reverse("report")).context
        self.assertEqual(context["total_revenue"], Decimal("75"))
        self.assertEqual(context["total_expenses"], Decimal("100"))
        self.assertEqual(context["projects_budget"][0]["spent"], Decimal("100"))

    def test_company_invoice_pages_load(self):
        self.login(self.fin_a)
        self.assertEqual(self.client.get(reverse("company_invoices", args=[self.company_a.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("select_project_invoices", args=[self.company_a.pk])).status_code, 200)

    def test_expense_invoice_page_shows_that_expenses_own_invoice(self):
        today = timezone.localdate()
        first, second = self.make_expense(status="approved"), self.make_expense(status="approved")
        for number, expense in (("FIRST-1", first), ("SECOND-2", second)):
            Invoice.objects.create(
                company=self.company_a, expense=expense, client=self.project.client, project=self.project,
                invoice_no=number, issue_date=today, due_date=today, subtotal=expense.amount, created_by=self.fin_a,
            )
        self.login(self.fin_a)
        page = self.client.get(reverse("expense-invoice-view", args=[second.pk]))
        self.assertContains(page, "SECOND-2")
        self.assertNotContains(page, "FIRST-1")


class TimesheetChecksTests(SecurityTestCase):
    def post(self, **override):
        self.login(self.eng_a)
        data = {"project": self.project.pk, "date": "2025-01-06", "hours_worked": "4"}
        data.update(override)
        return self.client.post(reverse("timesheet_add"), data)

    def test_valid_timesheet_saved(self):
        self.assertEqual(self.post().status_code, 302)

    def test_zero_future_and_before_project_start_rejected(self):
        self.project.start_date = datetime.date(2025, 1, 1)
        self.project.save()
        future = (timezone.localdate() + datetime.timedelta(days=2)).isoformat()
        for override in ({"hours_worked": "0"}, {"date": future}, {"date": "2024-12-01"}):
            self.assertEqual(self.post(**override).status_code, 200, override)
        self.assertEqual(Timesheet.objects.count(), 0)


class PayrollChecksTests(SecurityTestCase):
    def run_payroll(self, start, end):
        self.login(self.fin_a)
        return self.client.post(reverse("payroll_run_create"), {"period_start": start, "period_end": end})

    def log_full_days(self, profile, start, end):
        day = start
        while day <= end:
            if day.weekday() < 5:
                Timesheet.objects.create(employee=profile, project=self.project, date=day, hours_worked=Decimal("8"), status="approved")
            day += datetime.timedelta(days=1)

    def test_empty_period_is_refused_and_leaves_no_run(self):
        self.assertEqual(self.run_payroll("2025-03-01", "2025-03-31").status_code, 200)
        self.assertEqual(PayrollRun.objects.count(), 0)

    def test_overlapping_period_is_refused(self):
        self.log_full_days(self.eng_a_profile, datetime.date(2025, 3, 3), datetime.date(2025, 3, 7))
        self.assertEqual(self.run_payroll("2025-03-01", "2025-03-31").status_code, 302)
        self.assertEqual(self.run_payroll("2025-03-15", "2025-04-15").status_code, 200)
        self.assertEqual(PayrollRun.objects.count(), 1)

    def test_salary_across_two_months_uses_each_months_working_days(self):
        start, end = datetime.date(2025, 1, 16), datetime.date(2025, 2, 15)
        self.log_full_days(self.eng_a_profile, start, end)
        run = PayrollRun.objects.create(company=self.company_a, period_start=start, period_end=end, created_by=self.fin_a)
        generate_payroll(run)
        salary = self.eng_a_profile.base_salary
        jan_share = Decimal(_working_days(start, datetime.date(2025, 1, 31))) / Decimal(_working_days(datetime.date(2025, 1, 1), datetime.date(2025, 1, 31)))
        feb_share = Decimal(_working_days(datetime.date(2025, 2, 1), end)) / Decimal(_working_days(datetime.date(2025, 2, 1), datetime.date(2025, 2, 28)))
        expected = (salary * (jan_share + feb_share)).quantize(Decimal("0.01"))
        self.assertEqual(run.items.get(employee=self.eng_a_profile).gross_pay, expected)
