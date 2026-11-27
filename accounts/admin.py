from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Group, Permission
from django.utils.text import slugify

from .models import Role, User

# Roles may only grant permissions for the business apps, never for auth, admin or sessions.
ROLE_PERMISSION_APPS = ("finance", "workforce", "operations")


class CompanyAdminMixin:
    """Keep company admins inside their own company. Superusers see everything."""

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        if not request.user.company_id:
            return qs.none()
        return qs.filter(company_id=request.user.company_id)


@admin.register(User)
class CustomUserAdmin(CompanyAdminMixin, UserAdmin):
    ordering = ("email",)
    list_display = ("username", "email", "company", "is_staff", "is_active", "is_superuser")
    list_filter = ("company", "is_staff", "is_superuser")
    search_fields = ("email", "username", "first_name", "last_name")
    readonly_fields = ("created_by",)

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "username", "email", "password1", "password2",
                "first_name", "last_name", "company", "groups", "is_staff",
            ),
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            # A company admin must never see or edit a superuser account.
            qs = qs.filter(is_superuser=False)
        return qs

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            # Otherwise a company admin could make themselves a superuser or grant any permission.
            readonly += ["is_superuser", "user_permissions"]
        return readonly

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if not request.user.is_superuser and "company" in form.base_fields:
            form.base_fields["company"].widget = forms.HiddenInput()
        return form

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            obj.company = request.user.company
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "groups" and not request.user.is_superuser:
            if request.user.company_id:
                company_slug = slugify(request.user.company.name)
                kwargs["queryset"] = Group.objects.filter(name__endswith=f"_{company_slug}")
            else:
                kwargs["queryset"] = Group.objects.none()
        return super().formfield_for_manytomany(db_field, request, **kwargs)


@admin.register(Role)
class RoleAdmin(CompanyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name", "company__name")
    filter_horizontal = ("permissions",)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "permissions" and not request.user.is_superuser:
            kwargs["queryset"] = Permission.objects.filter(content_type__app_label__in=ROLE_PERMISSION_APPS)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "company" and not request.user.is_superuser:
            from operations.models import Company

            kwargs["queryset"] = Company.objects.filter(pk=request.user.company_id)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            obj.company_id = request.user.company_id
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        form.instance.get_or_create_permission_group()
        form.save()
