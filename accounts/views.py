import logging

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import DetailView

from accounts.models import User

logger = logging.getLogger(__name__)


def landing_view(request):
    return render(request, "landing.html")


LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


def _login_throttle_key(request):
    username = request.POST.get("username", "").strip().lower()
    return f"login-fail:{request.META.get('REMOTE_ADDR', '')}:{username}"


def _login_failures(key):
    try:
        return cache.get(key, 0)
    except Exception:  # the cache being down must not lock everyone out
        logger.warning("Login throttle cache unavailable", exc_info=True)
        return 0


def _record_login_failure(key):
    try:
        cache.add(key, 0, LOGIN_LOCKOUT_SECONDS)
        cache.incr(key)
    except Exception:
        logger.warning("Login throttle cache unavailable", exc_info=True)


def _clear_login_failures(key):
    try:
        cache.delete(key)
    except Exception:
        logger.warning("Login throttle cache unavailable", exc_info=True)


def _safe_next_url(request):
    """Only follow a ?next= that stays on this site."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return "dashboard"


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        key = _login_throttle_key(request)
        if _login_failures(key) >= LOGIN_MAX_FAILURES:
            messages.error(request, "Too many failed attempts. Please wait 15 minutes and try again.")
            form = AuthenticationForm(request)
            return render(request, "login.html", {"form": form, "next": _safe_next_url(request)}, status=429)

        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            _clear_login_failures(key)
            login(request, form.get_user())
            return redirect(_safe_next_url(request))
        _record_login_failure(key)
    else:
        form = AuthenticationForm(request)

    return render(request, "login.html", {"form": form, "next": _safe_next_url(request)})


@login_required(login_url="login")
@require_POST
def logout_view(request):
    logout(request)
    return redirect("home")


@login_required(login_url="login")
def dashboard_view(request):
    company = request.user.company
    if company is None:
        # No company assigned yet: show an empty dashboard instead of crashing.
        context = {
            "company": None,
            "active_employees": [],
            "active_employees_count": 0,
            "active_projects": [],
            "active_projects_count": 0,
        }
        return render(request, "dashboard.html", context)

    active_employees = company.employees.filter(is_active=True)
    active_projects = company.projects.filter(status="active")

    context = {
        "company": company,
        "active_employees": active_employees,
        "active_employees_count": active_employees.count(),
        "active_projects": active_projects,
        "active_projects_count": active_projects.count(),
    }
    return render(request, "dashboard.html", context)

class ProfileView(LoginRequiredMixin, DetailView):
    model = User
    template_name = "profile.html"

    def get_object(self):
        return self.request.user
