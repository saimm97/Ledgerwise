"""Shared access-control building blocks.

Every view that touches company data should use these, so the rules live in one place:

* anonymous visitors are sent to the login page;
* a signed-in user must belong to a company;
* an optional permission is required;
* querysets are filtered to the user's own company, so another company's records
  answer "404 Not Found" instead of revealing that they exist.
"""
from functools import wraps

from django.contrib.auth.mixins import AccessMixin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

# The existing menus show finance and payroll pages to anyone holding this permission,
# so the server enforces the same rule. Swap it here if a dedicated permission is added later.
FINANCE_MANAGER_PERM = "workforce.add_payrollrun"
MANAGE_PROJECT_PERM = "operations.change_project"


def get_profile(user):
    """The user's EmployeeProfile, or None when they do not have one."""
    return getattr(user, "employee_profile", None)


def user_manages_project(user, project):
    """True for the project's manager, or anyone allowed to change projects."""
    profile = get_profile(user)
    is_manager = profile is not None and project.project_manager_id == profile.pk
    return is_manager or user.has_perm(MANAGE_PROJECT_PERM)


def user_sees_project_finances(user, project):
    """Invoices, payments and amounts are limited to managers and the finance team."""
    return user.has_perm(FINANCE_MANAGER_PERM) or user_manages_project(user, project)


def authorize(user, *, perm=None, require_profile=False):
    """Raise PermissionDenied unless an authenticated user passes the checks."""
    if user.company_id is None:
        raise PermissionDenied("Your account is not linked to a company yet.")
    if perm and not user.has_perm(perm):
        raise PermissionDenied("You do not have permission to do that.")
    if require_profile and get_profile(user) is None:
        raise PermissionDenied("You need an employee profile to do that.")


class SecureViewMixin(AccessMixin):
    """Class-based views: login, company membership and an optional permission.

    Set ``required_perm`` and/or ``require_profile`` on the view.
    """
    required_perm = None
    require_profile = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        authorize(request.user, perm=self.required_perm, require_profile=self.require_profile)
        return super().dispatch(request, *args, **kwargs)

    @property
    def company(self):
        return self.request.user.company

    @property
    def profile(self):
        return get_profile(self.request.user)


class CompanyScopedMixin(SecureViewMixin):
    """Also limits ``get_queryset`` to the user's company (``company_field`` may span relations)."""
    company_field = "company"

    def get_queryset(self):
        return super().get_queryset().filter(**{self.company_field: self.request.user.company_id})


def secure_view(*, perm=None, require_profile=False):
    """The same checks as ``SecureViewMixin``, for function-based views."""
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            authorize(request.user, perm=perm, require_profile=require_profile)
            return view(request, *args, **kwargs)
        return wrapper
    return decorator
