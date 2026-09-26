# src/accounts/models.py
"""The user model. Four of the decisions in here cannot be undone by a migration.

DATA-MODEL-DRAFT.md is the authority on names: table `app_user`, and column names as
written there. It is NOT the authority on column types -- see the citext note below,
which is section 15 fault 17.
"""
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class AppUserManager(BaseUserManager):
    """Two creators and one lookup override. The override is the load-bearing one."""

    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("email is required: it is the login field")
        email = self.normalize_email(email).strip()
        user = self.model(email=email, **extra)
        user.set_password(password)        # never assign .password; it must be hashed
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        # The keyword NAMES are a contract with portal/bootstrap.py's ensure-admin, which
        # calls create_superuser(email=..., password=...) and passes nothing else.
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not (extra["is_staff"] and extra["is_superuser"]):
            raise ValueError("a superuser must have is_staff and is_superuser")
        return self._create(email, password, **extra)

    def get_by_natural_key(self, username):
        # ModelBackend authenticates through this, and the stock implementation is an
        # EXACT match. The unique index below is on Lower(email), so without __iexact
        # "Bob@x.com" would be refused at signup as a duplicate and refused at login as
        # unknown -- the two halves of the same defect, in opposite directions.
        return self.get(**{"%s__iexact" % self.model.USERNAME_FIELD: username})


class AppUser(AbstractBaseUser, PermissionsMixin):
    # unique=True is REDUNDANT with the constraint below and is not optional: Django's
    # auth.E003 system check fails the whole project when USERNAME_FIELD is not unique,
    # and total_unique_constraints -- what that check consults -- excludes EXPRESSION-based
    # UniqueConstraints. A system-check error is not a warning: manage.py refuses to run,
    # so collectstatic fails and there is no image.
    email = models.EmailField("email address", max_length=254, unique=True)
    display_name = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = AppUserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []               # createsuperuser then prompts for email + password

    class Meta:
        db_table = "app_user"          # section 15 fault 15: the draft's DDL is the authority
        constraints = [
            models.UniqueConstraint(Lower("email"), name="app_user_email_ci_unique"),
        ]

    def __str__(self):
        return self.email

    def get_short_name(self):
        return self.display_name or self.email.split("@")[0]

    def get_full_name(self):
        return self.display_name or self.email
