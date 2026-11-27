"""Regression tests for the security fixes: one test per hole that used to be open.

Run with:  python manage.py test Ledgerwise
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.admin import CustomUserAdmin
from accounts.models import Role, User
from finance.models import Expense, ExpenseCategory, ExpenseStatus, Invoice
from operations.models import Client, Company, EmployeeProject, Project
from workforce.models import EmployeeProfile, Timesheet

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD = "S3cure-pass-123"


def grant(user, *codenames):
    for codename in codenames:
        user.user_permissions.add(Permission.objects.get(codename=codename))


def make_company(name):
    return Company.objects.create(
        name=name, registration_no=f"REG-{name}", tax_id=f"TAX-{name}", email=f"{name}@example.com",
        phone="1", address="x", country="US", currency="USD", account_no="1",
    )


def make_person(company, username, *perms):
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password=PASSWORD, company=company)
    role, _ = Role.objects.get_or_create(name="Staff", company=company)
    role.get_or_create_permission_group()
    profile = EmployeeProfile.objects.create(
        user=user, company=company, role=role, job_title="Staff", employment_type="full_time", salary_type="fixed",
        base_salary=Decimal("1000"), joining_date=datetime.date(2024, 1, 1),
    )
    grant(user, *perms)
    return user, profile


@override_settings(CACHES=LOCMEM)
class SecurityTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company_a = make_company("alpha")
        cls.company_b = make_company("beta")

        cls.fin_a, cls.fin_a_profile = make_person(cls.company_a, "fin_a", "add_payrollrun", "change_project", "add_project")
        cls.pm_a, cls.pm_a_profile = make_person(cls.company_a, "pm_a", "change_project", "add_expense", "add_timesheet")
        cls.eng_a, cls.eng_a_profile = make_person(cls.company_a, "eng_a", "add_expense", "add_timesheet")
        cls.fin_b, cls.fin_b_profile = make_person(cls.company_b, "fin_b", "add_payrollrun", "change_project")

        client = Client.objects.create(company=cls.company_a, name="Client", email="client@example.com", phone="1")
        cls.project = Project.objects.create(
            company=cls.company_a, client=client, name="Alpha project", project_manager=cls.pm_a_profile, budget=Decimal("5000"),
        )
        EmployeeProject.objects.create(project=cls.project, employee=cls.eng_a_profile, role_in_project="Dev")
        cls.category = ExpenseCategory.objects.create(company=cls.company_a, name="Travel")

    def setUp(self):
        cache.clear()

    def make_expense(self, employee=None, **extra):
        return Expense.objects.create(
            company=self.company_a, project=self.project, employee=employee or self.eng_a_profile, category=self.category,
            amount=Decimal("100"), currency="USD", expense_date=datetime.date(2025, 1, 1), **extra,
        )

    def login(self, user):
        self.client.force_login(user)


class AccessControlTests(SecurityTestCase):
    def test_anonymous_cannot_see_report(self):
        response = self.client.get(reverse("report"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])

    def test_engineer_cannot_see_company_report(self):
        self.login(self.eng_a)
        self.assertEqual(self.client.get(reverse("report")).status_code, 403)

    def test_report_only_counts_own_company(self):
        self.make_expense()
        self.login(self.fin_b)
        response = self.client.get(reverse("report"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_expenses"], 0)

    def test_other_company_invoices_not_found(self):
        self.login(self.fin_b)
        response = self.client.get(reverse("company_invoices", args=[self.company_a.pk]))
        self.assertEqual(response.status_code, 404)

    def test_other_company_projects_not_found(self):
        self.login(self.fin_b)
        self.assertEqual(self.client.get(reverse("company_projects", args=[self.company_a.pk])).status_code, 404)

    def test_other_company_project_detail_not_found(self):
        self.login(self.fin_b)
        self.assertEqual(self.client.get(reverse("project_detail", args=[self.project.pk])).status_code, 404)

    def test_user_without_company_is_refused(self):
        orphan = User.objects.create_user(username="orphan", email="o@example.com", password=PASSWORD)
        self.login(orphan)
        self.assertEqual(self.client.get(reverse("report")).status_code, 403)

    def test_project_timeline_limited_to_manager_and_finance(self):
        url = reverse("project_timeline", args=[self.project.pk])
        self.login(self.eng_a)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.login(self.pm_a)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.login(self.fin_a)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_only_project_manager_can_assign_employees(self):
        url = reverse("project_detail", args=[self.project.pk])
        self.login(self.eng_a)
        response = self.client.post(url, {"employee": self.fin_a_profile.pk, "role_in_project": "Dev"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(EmployeeProject.objects.filter(employee=self.fin_a_profile).exists())

    def test_only_finance_can_run_payroll(self):
        self.login(self.eng_a)
        response = self.client.post(reverse("payroll_run_create"), {"period_start": "2025-01-01", "period_end": "2025-01-31"})
        self.assertEqual(response.status_code, 403)

    def test_payslip_of_another_company_is_refused(self):
        self.login(self.fin_b)
        self.assertEqual(self.client.get(reverse("employee_payroll_history", args=[self.eng_a_profile.pk])).status_code, 403)


@mock.patch("finance.views.send_invoice_email")
class ExpenseApprovalTests(SecurityTestCase):
    def approve(self, user, expense, status="approved"):
        self.login(user)
        return self.client.post(reverse("expense_approval_update", args=[expense.pk]), {"status": status})

    def test_outsider_cannot_approve(self, _mail):
        expense = self.make_expense()
        self.assertEqual(self.approve(self.fin_b, expense).status_code, 404)
        expense.refresh_from_db()
        self.assertEqual(expense.status, ExpenseStatus.SUBMITTED)

    def test_plain_engineer_cannot_approve(self, _mail):
        expense = self.make_expense()
        self.assertEqual(self.approve(self.eng_a, expense).status_code, 404)

    def test_manager_can_approve_once_and_one_invoice_is_made(self, mail):
        expense = self.make_expense()
        with self.captureOnCommitCallbacks(execute=True):
            self.approve(self.pm_a, expense)
        expense.refresh_from_db()
        self.assertEqual(expense.status, ExpenseStatus.APPROVED)
        self.assertEqual(expense.approved_by, self.pm_a)
        self.assertIsNotNone(expense.approved_at)
        self.assertEqual(Invoice.objects.filter(expense=expense).count(), 1)
        mail.delay.assert_called_once()

        # A second decision (double click, second reviewer) is refused: no flip, no second invoice.
        self.approve(self.pm_a, expense, status="rejected")
        expense.refresh_from_db()
        self.assertEqual(expense.status, ExpenseStatus.APPROVED)
        self.assertEqual(Invoice.objects.filter(expense=expense).count(), 1)

    def test_cannot_approve_own_expense(self, _mail):
        expense = self.make_expense(employee=self.pm_a_profile)
        self.assertEqual(self.approve(self.pm_a, expense).status_code, 404)

    def test_company_expense_needs_finance_permission(self, _mail):
        expense = Expense.objects.create(
            company=self.company_a, employee=self.eng_a_profile, category=self.category, amount=Decimal("5"),
            currency="USD", expense_date=datetime.date(2025, 1, 1),
        )
        self.login(self.pm_a)
        self.assertEqual(self.client.post(reverse("company-expense-approve", args=[expense.pk]), {"status": "approved"}).status_code, 403)


class ExpenseSubmissionTests(SecurityTestCase):
    """Submitting an expense must save it and redirect (this once crashed after saving)."""

    def test_employee_can_submit_project_expense(self):
        self.login(self.eng_a)
        response = self.client.post(reverse("expense-add"), {
            "project": self.project.pk, "category": self.category.pk, "amount": "25.00",
            "description": "Taxi", "expense_date": "2025-01-02",
        })
        self.assertRedirects(response, reverse("expense-list"), fetch_redirect_response=False)
        expense = Expense.objects.get(description="Taxi")
        self.assertEqual(expense.company, self.company_a)
        self.assertEqual(expense.employee, self.eng_a_profile)
        self.assertEqual(expense.status, ExpenseStatus.SUBMITTED)

    def test_employee_can_submit_company_expense(self):
        self.login(self.eng_a)
        response = self.client.post(reverse("company-expense-add"), {
            "category": self.category.pk, "amount": "9.50", "description": "Coffee", "expense_date": "2025-01-02",
        })
        self.assertRedirects(response, reverse("company-expense-list"), fetch_redirect_response=False)
        self.assertTrue(Expense.objects.filter(description="Coffee", project__isnull=True).exists())


class TimesheetTests(SecurityTestCase):
    def make_timesheet(self, employee):
        return Timesheet.objects.create(
            employee=employee, project=self.project, date=datetime.date(2025, 1, 2), hours_worked=Decimal("4"), status="submitted",
        )

    def test_manager_approves_once(self):
        sheet = self.make_timesheet(self.eng_a_profile)
        self.login(self.pm_a)
        self.client.post(reverse("approve_timesheet", args=[sheet.pk]))
        sheet.refresh_from_db()
        self.assertEqual(sheet.status, "approved")
        self.assertEqual(sheet.approved_by, self.pm_a)

    def test_outsider_cannot_approve(self):
        sheet = self.make_timesheet(self.eng_a_profile)
        self.login(self.fin_b)
        self.assertEqual(self.client.post(reverse("approve_timesheet", args=[sheet.pk])).status_code, 404)

    def test_engineer_cannot_approve_colleague(self):
        sheet = self.make_timesheet(self.pm_a_profile)
        self.login(self.eng_a)
        self.assertEqual(self.client.post(reverse("approve_timesheet", args=[sheet.pk])).status_code, 403)

    def test_manager_cannot_approve_own_timesheet(self):
        sheet = self.make_timesheet(self.pm_a_profile)
        self.login(self.pm_a)
        self.assertEqual(self.client.post(reverse("approve_timesheet", args=[sheet.pk])).status_code, 403)

    def test_cannot_edit_someone_elses_timesheet(self):
        sheet = self.make_timesheet(self.eng_a_profile)
        self.login(self.pm_a)
        self.assertEqual(self.client.get(reverse("timesheet_edit", args=[sheet.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("timesheet_delete", args=[sheet.pk])).status_code, 404)


class LoginTests(SecurityTestCase):
    def attempt(self, password, **extra):
        return self.client.post(reverse("login"), {"username": "eng_a", "password": password, **extra})

    def test_lockout_after_five_failures(self):
        for _ in range(5):
            self.assertEqual(self.attempt("wrong").status_code, 200)
        self.assertEqual(self.attempt("wrong").status_code, 429)
        # Even the right password is refused while locked out.
        self.assertEqual(self.attempt(PASSWORD).status_code, 429)

    def test_success_clears_failures(self):
        for _ in range(3):
            self.attempt("wrong")
        self.assertEqual(self.attempt(PASSWORD).status_code, 302)

    def test_next_must_stay_on_site(self):
        response = self.attempt(PASSWORD, next="https://evil.example.com/")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("evil.example.com", response["Location"])

    def test_next_on_site_is_followed(self):
        response = self.attempt(PASSWORD, next="/finance/expenses/")
        self.assertEqual(response["Location"], "/finance/expenses/")


class AdminHardeningTests(SecurityTestCase):
    def setUp(self):
        super().setUp()
        self.admin = CustomUserAdmin(User, AdminSite())
        self.factory = RequestFactory()

    def request_for(self, user):
        request = self.factory.get("/admin/")
        request.user = user
        return request

    def test_company_admin_cannot_grant_superuser_or_permissions(self):
        readonly = self.admin.get_readonly_fields(self.request_for(self.fin_a))
        self.assertIn("is_superuser", readonly)
        self.assertIn("user_permissions", readonly)

    def test_company_admin_does_not_see_superusers_or_other_companies(self):
        root = User.objects.create_superuser(username="root", email="root@example.com", password=PASSWORD, company=self.company_a)
        users = self.admin.get_queryset(self.request_for(self.fin_a))
        self.assertNotIn(root, users)
        self.assertNotIn(self.fin_b, users)
        self.assertIn(self.eng_a, users)

    def test_new_user_is_not_forced_to_staff(self):
        request = self.request_for(self.fin_a)
        user = User(username="new", email="new@example.com", company=self.company_a)
        self.admin.save_model(request, user, form=None, change=False)
        self.assertFalse(user.is_staff)


class SettingsTests(TestCase):
    def test_missing_required_setting_fails_loudly(self):
        from Ledgerwise.settings.base import require_env

        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(ImproperlyConfigured):
            require_env("SECRET_KEY")

    def test_env_helpers(self):
        from Ledgerwise.settings.base import env_bool, env_list

        with mock.patch.dict("os.environ", {"X": "True", "Y": "a, b ,,c"}):
            self.assertTrue(env_bool("X"))
            self.assertFalse(env_bool("MISSING"))
            self.assertEqual(env_list("Y"), ["a", "b", "c"])
