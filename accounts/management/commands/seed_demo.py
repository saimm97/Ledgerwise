"""
Fill the database with demo data so every screen has something to show.

Usage (Docker):
    docker compose exec server python manage.py seed_demo
        -> creates demo@ledgerwise.test / Demo@12345 (not a superuser)
    docker compose exec server python manage.py seed_demo --email you@example.com
        -> attaches the demo data to an existing user instead

Safe to run more than once: it skips anything that already exists.
Only runs when DEBUG is on, unless you pass --force.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Role, User
from finance.models import (
    Expense, ExpenseCategory, Invoice, InvoiceItem, Payment, ProjectPaymentInvoice,
)
from operations.models import Client, Company, EmployeeProject, Project
from workforce.models import EmployeeProfile, PayrollItem, PayrollRun, Timesheet
from workforce.services import generate_payroll

DEMO_PASSWORD = "Demo@12345"
DEMO_EMAIL = "demo@ledgerwise.test"


class Command(BaseCommand):
    help = "Create demo company data and attach it to an existing user."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Email of the user to attach the data to (default: first superuser).")
        parser.add_argument("--reset-password", action="store_true", help="Reset the demo account password to the default.")
        parser.add_argument("--company", default="Northwind Labs", help="Company name to create or reuse.")
        parser.add_argument("--force", action="store_true", help="Allow running when DEBUG is off.")

    @transaction.atomic
    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["force"]:
            raise CommandError("Refusing to seed demo data while DEBUG is off. Use --force if you really mean it.")

        if opts["email"]:
            user = User.objects.filter(email=opts["email"]).first()
            if not user:
                raise CommandError(f"No user with email {opts['email']}.")
        else:
            # Default: a regular (non-superuser) demo account with a known password.
            user, created = User.objects.get_or_create(
                email=DEMO_EMAIL, defaults=dict(username="demo", first_name="Sam", last_name="Morgan"))
            if created or opts["reset_password"]:
                user.set_password(DEMO_PASSWORD)
                user.save()

        today = timezone.localdate()
        ago = lambda d: today - timedelta(days=d)

        # ---- Company ----
        company, _ = Company.objects.get_or_create(
            name=opts["company"],
            defaults=dict(registration_no="REG-NW-001", tax_id="TAX-NW-001", email="finance@northwind.test",
                          phone="+1 555 0100", address="1 Market Street, San Francisco", country="US",
                          currency="USD", account_no="000123456789"),
        )
        user.company = company
        user.is_staff = True
        if not user.first_name:
            user.first_name = "Sam"
        if not user.last_name:
            user.last_name = "Morgan"
        user.save()

        # ---- Roles (the Finance Manager role unlocks payroll and approval menus) ----
        finance_perms = Permission.objects.filter(
            content_type__app_label__in=["finance", "workforce", "operations"], codename__regex=r"^(add|change|view)_")
        staff_perms = Permission.objects.filter(
            codename__in=["add_expense", "view_expense", "add_timesheet", "view_timesheet", "view_project"])
        roles = {}
        for name, perms in (("Finance Manager", finance_perms), ("Engineer", staff_perms)):
            role, _ = Role.objects.get_or_create(name=name, company=company)
            role.permissions.set(perms)
            role.get_or_create_permission_group()
            roles[name] = role

        # ---- People ----
        def profile(u, title, role, salary_type, amount, joined):
            kwargs = dict(company=company, role=role, job_title=title, employment_type="full_time",
                          salary_type=salary_type, joining_date=joined,
                          base_salary=amount if salary_type == "fixed" else None,
                          hourly_rate=amount if salary_type == "hourly" else None)
            p = EmployeeProfile.objects.filter(user=u).first()
            if p:
                return p
            return EmployeeProfile.objects.create(user=u, **kwargs)

        def person(username, first, last, title, salary_type, amount):
            u, created = User.objects.get_or_create(
                email=f"{username}@northwind.test",
                defaults=dict(username=username, first_name=first, last_name=last, company=company, created_by=user))
            if created:
                u.set_password(DEMO_PASSWORD)
                u.save()
            return profile(u, title, roles["Engineer"], salary_type, Decimal(amount), ago(400))

        me = profile(user, "Finance Manager", roles["Finance Manager"], "fixed", Decimal("8500.00"), ago(700))
        ava = person("ava.khan", "Ava", "Khan", "Senior Engineer", "hourly", "48.00")
        liam = person("liam.ortiz", "Liam", "Ortiz", "Product Designer", "hourly", "42.00")
        noor = person("noor.ali", "Noor", "Ali", "Engineering Lead", "fixed", "7200.00")
        team = [ava, liam, noor]

        # ---- Clients and projects ----
        clients = {}
        for name, email in (("Acme Corporation", "ap@acme.test"), ("Globex Industries", "billing@globex.test"),
                            ("Initech", "accounts@initech.test"), ("Umbrella Logistics", "pay@umbrella.test")):
            clients[name], _ = Client.objects.get_or_create(
                company=company, name=name, defaults=dict(email=email, phone="+1 555 0111", address="Main Street"))

        project_specs = [
            ("Mobile app", "Acme Corporation", "Customer app for iOS and Android.", 60000, noor, "active", 160, None),
            ("Data migration", "Globex Industries", "Move the legacy warehouse to the cloud.", 45000, me, "active", 120, 20),
            ("Website revamp", "Initech", "New marketing site and CMS.", 25000, noor, "active", 90, 45),
            ("ERP rollout", "Umbrella Logistics", "Roll out the ERP to three regions.", 90000, me, "planned", 10, 200),
        ]
        projects = {}
        for name, client, desc, budget, pm, status, start, end in project_specs:
            projects[name], _ = Project.objects.get_or_create(
                company=company, name=name,
                defaults=dict(client=clients[client], description=desc, budget=Decimal(budget), project_manager=pm,
                              status=status, start_date=ago(start), end_date=(today + timedelta(days=end)) if end else None))
        for p in projects.values():
            for emp, role_name in ((ava, "Developer"), (liam, "Designer"), (noor, "Lead")):
                EmployeeProject.objects.get_or_create(employee=emp, project=p, defaults=dict(role_in_project=role_name))

        # ---- Expense categories and expenses ----
        cats = {n: ExpenseCategory.objects.get_or_create(company=company, name=n)[0]
                for n in ("Contractor fees", "Cloud hosting", "Travel", "Software", "Other")}
        if not Expense.objects.filter(company=company).exists():
            now = timezone.now()
            rows = [  # project, category, amount, days ago, status, who
                ("Mobile app", "Contractor fees", 18500, 6, "approved", ava), ("Mobile app", "Cloud hosting", 4200, 14, "approved", ava),
                ("Mobile app", "Travel", 1850, 20, "approved", noor), ("Data migration", "Cloud hosting", 9600, 9, "approved", noor),
                ("Data migration", "Contractor fees", 14200, 33, "approved", liam), ("Website revamp", "Software", 2300, 12, "approved", liam),
                ("Website revamp", "Contractor fees", 8800, 40, "approved", liam), ("ERP rollout", "Other", 1500, 5, "approved", noor),
                ("Mobile app", "Software", 1380, 3, "submitted", ava), ("Data migration", "Travel", 940, 2, "submitted", noor),
                ("Website revamp", "Other", 260, 8, "rejected", liam),
            ]
            for proj, cat, amount, days, status, who in rows:
                Expense.objects.create(
                    company=company, project=projects[proj], employee=who, category=cats[cat], amount=Decimal(amount),
                    currency="USD", description=f"{cat} for {proj}", expense_date=ago(days), status=status,
                    approved_by=user if status != "submitted" else None, approved_at=now if status != "submitted" else None)

        # ---- Timesheets (last 3 weeks of weekdays) ----
        if not Timesheet.objects.filter(employee__company=company).exists():
            plist = list(projects.values())[:3]
            for i in range(1, 22):
                d = ago(i)
                if d.weekday() >= 5:
                    continue
                for n, emp in enumerate(team):
                    old = i > 7
                    Timesheet.objects.create(
                        employee=emp, project=plist[(i + n) % 3], date=d, hours_worked=Decimal("7.50") if n else Decimal("8.00"),
                        status="approved" if old else "submitted", approved_by=user if old else None,
                        approved_at=timezone.now() if old else None)

        # ---- Client billing: invoices and payments ----
        if not ProjectPaymentInvoice.objects.filter(company=company).exists():
            specs = [  # no, project, amount, tax %, issued days ago, due in days, payments (confirmed)
                ("INV-0038", "Website revamp", 5600, 0, 29, -14, []),
                ("INV-0039", "Data migration", 21000, 0, 27, -10, []),
                ("INV-0040", "Website revamp", 3900, 0, 15, 15, [1500]),
                ("INV-0041", "Data migration", 8250, 5, 9, 21, []),
                ("INV-0042", "Mobile app", 12400, 0, 6, 24, [12400]),
                ("INV-0043", "Mobile app", 15000, 8, 60, -30, [16200]),
            ]
            for no, proj, amount, tax, issued, due, pays in specs:
                inv = ProjectPaymentInvoice.objects.create(
                    company=company, project=projects[proj], invoice_no=no, issue_date=ago(issued),
                    due_date=today + timedelta(days=due), amount=Decimal(amount), tax_percentage=Decimal(tax),
                    description=f"{proj} milestone", invoice_sent=True, created_by=user)
                for amt in pays:
                    Payment.objects.create(company=company, invoice=inv, amount=Decimal(amt), currency="USD",
                                           status="confirmed", paid_at=timezone.now(), payment_method="bank_transfer",
                                           transaction_ref=f"TXN-{no}", created_by=user, received_by=user)
                inv.update_payment_status()

        # ---- Expense invoices with line items (the older invoice screens) ----
        if not Invoice.objects.filter(company=company).exists():
            for no, client, proj, items in (
                ("EXP-1001", "Acme Corporation", "Mobile app", [("Cloud hosting, October", "Cloud hosting", 3, "1400.00"), ("Store listing fees", "Other", 1, "300.00")]),
                ("EXP-1002", "Globex Industries", "Data migration", [("Migration contractors", "Contractor fees", 2, "4800.00")]),
            ):
                inv = Invoice.objects.create(company=company, client=clients[client], project=projects[proj], invoice_no=no,
                                             issue_date=ago(10), due_date=today + timedelta(days=20),
                                             subtotal=sum(Decimal(q) * Decimal(p) for _, _, q, p in items), status="sent",
                                             invoice_sent=True, created_by=user)
                for desc, cat, q, p in items:
                    InvoiceItem.objects.create(invoice=inv, description=desc, category=cat, quantity=Decimal(q), unit_price=Decimal(p))

        # ---- Invoices linked to individual expenses (powers the See / Download Invoice buttons) ----
        linked = Invoice.objects.filter(company=company, expense__isnull=False).count()
        todo = Expense.objects.filter(company=company, status="approved", invoice__isnull=True, project__isnull=False).order_by("expense_date")[:max(0, 6 - linked)]
        for exp in todo:
            inv = Invoice.objects.create(
                company=company, expense=exp, client=exp.project.client, project=exp.project,
                invoice_no=f"EXI-{str(exp.id)[:8].upper()}", issue_date=exp.expense_date,
                due_date=exp.expense_date + timedelta(days=30), subtotal=exp.amount, status="sent",
                invoice_sent=True, created_by=user)
            InvoiceItem.objects.create(invoice=inv, description=exp.description, category=exp.category.name,
                                       quantity=Decimal("1"), unit_price=exp.amount)

        # ---- Months of history: timesheets, client billing and payslips, so the project
        #      monthly view and the pay history have something to show ----
        if not Timesheet.objects.filter(employee__company=company, date__lt=ago(25)).exists():
            live = [p for p in projects.values() if p.status == "active"]
            people = [me] + team
            for i in range(25, 126):
                d = ago(i)
                if d.weekday() >= 5:
                    continue
                for n, emp in enumerate(people):
                    proj = live[(i + n) % len(live)]
                    if proj.start_date and d < proj.start_date:
                        proj = live[0] if not live[0].start_date or d >= live[0].start_date else None
                    if proj is None:
                        continue
                    Timesheet.objects.create(
                        employee=emp, project=proj, date=d, hours_worked=Decimal("8.00") if n % 2 == 0 else Decimal("7.50"),
                        status="approved", approved_by=user, approved_at=timezone.now())
            # run the real payroll code for each of the last three full months
            first_this = today.replace(day=1)
            for back in (3, 2, 1):
                y, m = first_this.year, first_this.month - back
                while m <= 0:
                    y, m = y - 1, m + 12
                start = date(y, m, 1)
                end = (date(y + (m == 12), 1 if m == 12 else m + 1, 1)) - timedelta(days=1)
                if PayrollRun.objects.filter(company=company, period_start=start).exists():
                    # an older seed already made this run by hand: its hours are paid
                    Timesheet.objects.filter(employee__company=company, date__gte=start, date__lte=end,
                                             status="approved").update(payed_status="paid")
                else:
                    run = PayrollRun.objects.create(company=company, period_start=start, period_end=end, created_by=user)
                    generate_payroll(run)
                    for item in run.items.all():
                        item.deductions = (item.gross_pay * Decimal("0.12")).quantize(Decimal("0.01"))
                        item.save()

        if not ProjectPaymentInvoice.objects.filter(company=company, issue_date__lt=ago(70)).exists():
            for no, proj, amount, issued in (
                ("INV-0030", "Mobile app", 14000, 150), ("INV-0031", "Mobile app", 16500, 115),
                ("INV-0032", "Data migration", 9800, 105), ("INV-0033", "Data migration", 12000, 75),
                ("INV-0034", "Website revamp", 6200, 85), ("INV-0035", "Mobile app", 13200, 80)):
                inv = ProjectPaymentInvoice.objects.create(
                    company=company, project=projects[proj], invoice_no=no, issue_date=ago(issued),
                    due_date=ago(issued - 30), amount=Decimal(amount), tax_percentage=Decimal(0),
                    description=f"{proj} milestone", invoice_sent=True, created_by=user)
                Payment.objects.create(company=company, invoice=inv, amount=Decimal(amount), currency="USD", status="confirmed",
                                       paid_at=timezone.now() - timedelta(days=issued - 12), payment_method="bank_transfer",
                                       transaction_ref=f"TXN-{no}", created_by=user, received_by=user)
                inv.update_payment_status()

        # payslips created before the snapshot fields existed: fill them in from the employee record and timesheets
        from django.db.models import Sum
        for item in PayrollItem.objects.filter(payroll_run__company=company, salary_type=""):
            r, e = item.payroll_run, item.employee
            hrs = (Timesheet.objects.filter(employee=e, date__gte=r.period_start, date__lte=r.period_end, status="approved")
                   .aggregate(t=Sum("hours_worked"))["t"] or Decimal("152"))
            days = sum(1 for k in range((r.period_end - r.period_start).days + 1)
                       if (r.period_start + timedelta(days=k)).weekday() < 5)
            item.job_title, item.salary_type, item.base_salary, item.hourly_rate = e.job_title, e.salary_type, e.base_salary, e.hourly_rate
            item.hours_worked, item.working_days, item.expected_hours = hrs, days, Decimal(days * 8)
            item.save()

        self.stdout.write(self.style.SUCCESS(f"Demo data ready for {user.email} in company '{company.name}'."))
        if not opts["email"]:
            self.stdout.write(self.style.SUCCESS(f"LOGIN: {DEMO_EMAIL} (username: demo)  password: {DEMO_PASSWORD}"))
        self.stdout.write(f"Other demo logins use the password {DEMO_PASSWORD}: ava.khan / liam.ortiz / noor.ali (@northwind.test emails).")
