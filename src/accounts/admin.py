# src/accounts/admin.py
"""Admin for the custom user + the demo-session shim.

Passwords are NOT managed here. `AppUser.password` is a hash, and this admin deliberately omits
it from the form, so saving a user preserves the stored hash and the admin can never expose or
overwrite it with cleartext. Provision credentials with `manage.py createsuperuser` or the
bootstrap ensure-admin step (docs/index.md). The admin's job is to inspect accounts and manage
active/staff/role flags and the profile.
"""
from django.contrib import admin

from .models import AppUser, DemoSession


@admin.register(AppUser)
class AppUserAdmin(admin.ModelAdmin):
    list_display = ("email", "display_name", "is_active", "is_staff",
                    "is_superuser", "date_joined")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "display_name")           # also backs autocomplete_fields elsewhere
    ordering = ("email",)
    readonly_fields = ("last_login", "date_joined")
    filter_horizontal = ("groups", "user_permissions")
    fieldsets = (
        (None, {"fields": ("email", "display_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser",
                                     "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )


@admin.register(DemoSession)
class DemoSessionAdmin(admin.ModelAdmin):
    list_display = ("token", "label", "user")
    search_fields = ("token", "label", "user__email")
    autocomplete_fields = ("user",)
