# Ledgerwise

Ledgerwise is a comprehensive, Django-based financial management platform designed to streamline project budgeting, expense tracking, and workforce management for companies. It provides a multi-user environment with role-based access control, allowing for efficient collaboration between administrators, project managers, and employees.

## Features

- **Multi-Company Architecture:** Each instance of Ledgerwise can manage multiple companies, with data isolated between them.
- **Role-Based Access Control (RBAC):**  Define custom roles and permissions for users within a company, ensuring that users can only access the features and data relevant to their position.
- **Project Management:** Create and manage projects, assign project managers, and track project status and budget.
- **Financial Management:**
    - **Expense Tracking:** Employees can submit expenses, which can then be approved or rejected by authorized users.
    - **Invoicing:** Create and manage invoices for clients, track their status (draft, sent, paid), and record payments.
- **Workforce Management:**
    - **Employee Profiles:** Maintain detailed profiles for each employee, including their role, job title, and salary information.
    - **Timesheets:** Employees can submit timesheets for the hours they've worked on specific projects.
    - **Payroll Processing:** Run payroll for a given period, calculating gross and net pay for each employee.
- **Admin Panel:** A user-friendly admin panel (powered by `django-jazzmin` and `django-unfold`) for managing all aspects of the application.

## Roles

### Admin Panel

The Django admin panel provides a comprehensive interface for superusers to manage all data in the application, including:

- **User and Role Management:** Create and manage users, roles, and permissions.
- **Company and Project Management:**  Set up new companies, create projects, and manage project details.
- **Financial Oversight:**  View and manage all expenses, invoices, and payments across all companies.
- **Workforce Administration:** Manage employee profiles, timesheets, and payroll runs.

### Frontend (MVT)

The frontend of the application is built using Django's Model-View-Template (MVT) architecture. This provides a user-friendly interface for non-admin users to interact with the system.

- **Employees:** Can submit expenses and timesheets, and view their own payroll information.
- **Project Managers:** Can manage the projects they are assigned to, approve expenses and timesheets for their projects, and view project-related financial data.
- **Finance Managers/Accountants:** Can manage invoices, record payments, and run payroll.

## Workflow

The application follows a structured workflow, with different roles performing specific tasks.

### 1. Superadmin Setup

1.  **Create a Superadmin:** The first step is to create a superadmin account using the `createsuperuser` management command.
2.  **Log in to Admin Panel:** The superadmin logs into the Django admin panel.
3.  **Create a Company:** The superadmin creates a new company and adds its details.
4.  **Create a Company Admin:** The superadmin creates a new user who will be the administrator for the company.
5.  **Assign Permissions:** The superadmin creates a `company_admin` role with the necessary permissions and assigns this role to the company admin user.

### 2. Company Admin Setup (from Admin Panel)

1.  **Log in to Admin Panel:** The company admin logs into the Django admin panel.
2.  **Add Users and Employees:** The company admin adds new users and creates `EmployeeProfile`s for them.
3.  **Create Custom Roles:** The company admin creates custom roles with specific permissions for different types of employees within the company.
4.  **Assign Roles:** The company admin assigns these roles to the employees.
5.  **Create Projects and Clients:** The company admin creates new projects, adds clients for those projects, and assigns a project manager to each project.
6.  **Assign Employees to Projects:** The company admin assigns employees to the relevant projects.

### 3. Standard User Actions (from Frontend)

Once the setup is complete, standard users can perform their tasks through the frontend of the application.

-   **Employees:**
    -   Log in to the application.
    -   Add timesheets for the hours they've worked.
    -   Submit expenses.
    -   View their personal details and payroll information.
-   **Project Managers:**
    -   Log in to the application.
    -   Review and approve or reject timesheets submitted by employees for their projects.
    -   Review and approve or reject expenses submitted for their projects.
-   **Accountants/Finance Managers:**
    -   Log in to the application.
    -   Generate payroll for the company for a given period.
    -   Manage invoices and record payments.

## Models and Features

### Accounts

- **User:** Extends the default Django `User` model with a UUID primary key, an email as the unique identifier, and a link to the company they belong to.
- **Role:**  Defines a role within a company, with a set of associated permissions. Each role is linked to a Django `Group` to enforce access control.

### Operations

- **Company:** Represents a company using the platform. Contains basic company information such as name, registration number, and contact details.
- **Client:** Represents a client of a company.
- **Project:** Represents a project undertaken by a company for a client. It has a budget, a start and end date, and a status (planned, active, completed).
- **EmployeeProject:** A through model that links an employee to a project, defining their role in that project.

### Finance

- **ExpenseCategory:** Categories for expenses (e.g., travel, supplies).
- **Expense:** Represents an expense submitted by an employee for a specific project. It has a status (submitted, approved, rejected) and is linked to the employee who submitted it and the user who approved it.
- **Invoice:** Represents an invoice sent to a client for a project. It has a status (draft, sent, partial, paid, overdue) and includes details such as subtotal, tax, and total amount.
- **InvoiceItem:** Represents a line item on an invoice.
- **Payment:** Represents a payment received for an invoice.

### Workforce

- **EmployeeProfile:**  Contains detailed information about an employee, including their job title, employment type (full-time, contract), salary type (fixed, hourly), and joining date.
- **Timesheet:**  Represents a timesheet submitted by an employee for the hours they've worked on a specific project. It has a status (submitted, approved).
- **PayrollRun:** Represents a payroll run for a specific period for a company.
- **PayrollItem:** Represents the payroll details for a single employee for a specific payroll run, including gross pay, deductions, and net pay.

## Technology Stack

- **Backend:**
    - [Django](https://www.djangoproject.com/): A high-level Python Web framework.
    - [Django REST framework](https://www.django-rest-framework.org/): A powerful and flexible toolkit for building Web APIs (although this project seems to be primarily MVT).
    - `django-jazzmin`: For a modern, responsive admin theme.
    - `django-unfold`: For a modern, responsive admin theme.
- **Database:**
    - [PostgreSQL](https://www.postgresql.org/): A powerful, open source object-relational database system.
- **Containerization:**
    - [Docker](https://www.docker.com/): For containerizing and running the application.
    - [Docker Compose](https://docs.docker.com/compose/): For defining and running multi-container Docker applications.



## How to Run the App

### Quick start (recommended): Docker Compose

This is the easiest way. It starts Django, PostgreSQL, Redis and the Celery worker together, and runs migrations for you. You only need [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed.

1. **Go to the project folder:**
    ```bash
    cd <project-folder>
    ```

2. **Create a `.env` file** in the project root with this content:
    ```env
    SECRET_KEY=change-me-to-any-long-random-string
    POSTGRES_DB=ledgerwise_db
    POSTGRES_USER=ledgerwise_user
    POSTGRES_PASSWORD=12345678
    POSTGRES_HOST=db
    POSTGRES_PORT=5432
    REDIS_URL=redis://redis:6379/0

    # Email (optional, only needed to send invoice emails)
    EMAIL_HOST_USER=
    EMAIL_HOST_PASSWORD=
    ```

3. **Build and start everything:**
    ```bash
    docker compose up --build
    ```
    Wait until you see `Starting development server at http://0.0.0.0:8000/`.

4. **Create your first admin user** (in a second terminal):
    ```bash
    docker compose exec server python manage.py createsuperuser
    ```

5. **Open the app:**
    - App: [http://localhost:8000/](http://localhost:8000/)
    - Admin panel: [http://localhost:8000/admin/](http://localhost:8000/admin/)

6. **Stop it:**
    ```bash
    docker compose down          # keep the database
    docker compose down -v       # also delete the database
    ```

Notes:
- The project folder is mounted into the container, so template and CSS changes show up after a browser refresh (hard refresh with Cmd/Ctrl + Shift + R if the old styles are cached). Python changes reload automatically.
- `docker-compose-dev.yaml` is a different file: it pulls the prebuilt image from Docker Hub instead of building your local code. Do not use it to test local changes.
- If port 5432 or 6379 is already used on your machine (for example a local PostgreSQL), stop that service or change the left side of the `ports:` lines in `docker-compose.yaml`.

### Run without Docker

Use this if you prefer a normal Python setup. You need Python 3.12, PostgreSQL and Redis installed locally. PDF invoices also need the WeasyPrint system libraries (on macOS: `brew install pango`).

1. **Create and activate a virtual environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate      # macOS/Linux
    # venv\Scripts\activate        # Windows
    ```

2. **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3. **Create the database:**
    ```sql
    CREATE DATABASE ledgerwise_db;
    CREATE USER ledgerwise_user WITH PASSWORD '12345678';
    GRANT ALL PRIVILEGES ON DATABASE ledgerwise_db TO ledgerwise_user;
    ALTER DATABASE ledgerwise_db OWNER TO ledgerwise_user;
    ```

4. **Create a `.env` file** in the project root:
    ```env
    SECRET_KEY=change-me-to-any-long-random-string
    DB_NAME=ledgerwise_db
    DB_USER=ledgerwise_user
    DB_PASSWORD=12345678
    DB_HOST=localhost
    DB_PORT=5432

    # Email (optional)
    EMAIL_HOST_USER=
    EMAIL_HOST_PASSWORD=
    ```
    Redis is expected at `redis://localhost:6379/0` by default.

5. **Start Redis:**
    ```bash
    brew services start redis          # macOS
    # sudo service redis-server start  # Linux
    ```

6. **Tell Django to use the local settings** (do this in every new terminal, before any `manage.py` command):
    ```bash
    export DJANGO_SETTINGS_MODULE=Ledgerwise.settings.local
    ```

7. **Set up the database and an admin user:**
    ```bash
    python manage.py migrate
    python manage.py createsuperuser
    ```

8. **Start the server:**
    ```bash
    python manage.py runserver
    ```
    Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/).

9. **Start the Celery worker** (optional, only needed for background emails), in another terminal with the same `DJANGO_SETTINGS_MODULE`:
    ```bash
    celery -A Ledgerwise worker --loglevel=info
    ```

## User Interface

The interface is dark by default. Use the sun/moon button in the top bar (or on the login page) to switch between dark and light mode. Your choice is saved in the browser. All styling lives in `static/css/global.css`; shared layout is in `templates/base.html` and `templates/partials/`.

## Project monthly view and payslips

- **Project monthly view:** open any project and click "Monthly hours & invoices". It shows, month by month since the project started, the hours each person logged, what was invoiced, what was received, what is still owed and the approved expenses.
- **Payslips:** every payroll run keeps a snapshot of each person's pay (type, rate, hours, working days), so old payslips never change when pay rates are edited later. Open "My payroll" for your own payslips, or "Payroll history" (payroll managers) for every run and each person's full history. Each payslip can be downloaded as a PDF.

## Troubleshooting

- **`ModuleNotFoundError` or "settings not configured" when running `manage.py`:** you skipped step 6. Export `DJANGO_SETTINGS_MODULE=Ledgerwise.settings.local` first.
- **Login page shows but styles look old:** hard refresh the browser to clear the cached CSS.
- **`could not connect to server` (database):** PostgreSQL is not running, or the `.env` values do not match the database you created.
- **PDF invoice download fails with a missing library error:** install the WeasyPrint system libraries (macOS: `brew install pango`). Docker already includes them.
