# src/portal/settings.py
import os, pathlib
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent          # /app/src

def _flag(name, default="0"):
    return os.environ.get(name, default) not in ("", "0", "false", "False")

DEBUG = _flag("DOGFOOD_DEBUG")

# DEMO auth shim switch. Gated on its OWN flag, never DEBUG: the acceptance stack runs
# DOGFOOD_DEBUG=0 but still needs the cookie->user shim, and production must be able to run
# DOGFOOD_DEMO=0 with DEBUG=0. The eval compose sets DOGFOOD_DEMO=1; prod docs say =0.
DOGFOOD_DEMO = _flag("DOGFOOD_DEMO")

# SECRET_KEY: environment, then the state volume, then an ephemeral key so that
# `collectstatic` can run at BUILD time when neither exists. The entrypoint always writes
# the state file before gunicorn starts, so a served request never uses an ephemeral key.
_state = pathlib.Path("/state/secret_key")
SECRET_KEY = (os.environ.get("DJANGO_SECRET_KEY")
              or (_state.read_text().strip() if _state.is_file() else None)
              or "ephemeral-" + os.urandom(32).hex())

ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
if os.environ.get("DOGFOOD_HOST"):
    ALLOWED_HOSTS.append(os.environ["DOGFOOD_HOST"])

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "accounts", "audit", "events", "submissions", "judging", "normalize", "gallery",
]

MIDDLEWARE = [                                                  # §6: this order, all stock
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # DemoAuth runs AFTER AuthenticationMiddleware (so request.user already exists) and is a
    # no-op unless DOGFOOD_DEMO is on and a known `session=` cookie is present. §1 #1.
    "portal.middleware.DemoAuthMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "portal.middleware.ContentSecurityPolicyMiddleware",        # see §15 fault 5
]

DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "HOST": os.environ.get("DOGFOOD_DB_HOST", "db"),
    "PORT": os.environ.get("DOGFOOD_DB_PORT", "5432"),
    "NAME": os.environ.get("DOGFOOD_DB_NAME", "dogfood"),
    "USER": os.environ.get("DOGFOOD_DB_USER", "dogfood"),
    "PASSWORD": os.environ.get("DOGFOOD_DB_PASSWORD", "dogfood-local-only-change-me"),
    "CONN_MAX_AGE": 60,
}}

CACHES = {"default": {
    "BACKEND": "django.core.cache.backends.db.DatabaseCache",
    "LOCATION": "dogfood_cache",              # created by `createcachetable`, step 5
    "OPTIONS": {"MAX_ENTRIES": 10000, "CULL_FREQUENCY": 3},
}}
# NOT LocMemCache. Rate limits counted per-process are not rate limits: with
# DOGFOOD_WORKERS=2 a limit of 10 per minute becomes 20. THREAT-MODEL-DRAFT.md §14.

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": (
        "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG else
        "whitenoise.storage.CompressedManifestStaticFilesStorage")},
}

# Seven names below belong to `startproject`'s template, and they are typed here because this
# file replaces the file that had them. Measured from a real `startproject` under Django 5.2.17
# -- the series §6's specifier resolves -- on 2026-09-05, not recalled: the stock
# `context_processors` list is three entries, and a fourth one typed from memory is a name that
# does not exist. Two of the seven repeat a Django default; they are written anyway, so the set
# of names in this module can be diffed against the set the tool generates. §15 fault 23.
ROOT_URLCONF = "portal.urls"
WSGI_APPLICATION = "portal.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],         # stock is [], and APP_DIRS searches only each
    "APP_DIRS": True,                         # app's own templates/ -- so src/templates/ is
    "OPTIONS": {"context_processors": [       # unreachable without this (§15 fault 19)
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

STATIC_URL = "static/"
STATIC_ROOT = "/app/staticfiles"              # outside src/, so the dev bind mount cannot
STATICFILES_DIRS = [BASE_DIR / "static"]      # shadow the collectstatic output

AUTH_USER_MODEL = "accounts.AppUser"
LOGIN_URL = "/accounts/login/"
AUTH_PASSWORD_VALIDATORS = [                  # stock, and all four: this is the only password
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]                                             # policy the invite flow (§20) will ever have
LANGUAGE_CODE = "en-us"
USE_I18N = True
USE_TZ = True                                 # no naive datetimes anywhere
TIME_ZONE = "UTC"                             # display timezone is per-event, on the model

if _flag("DOGFOOD_TLS"):                      # four settings, one switch
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    if os.environ.get("DOGFOOD_HOST"):
        CSRF_TRUSTED_ORIGINS = ["https://" + os.environ["DOGFOOD_HOST"]]

DOGFOOD_RATE_LIMITS = {                       # [T-0] names follow THREAT-MODEL §14's actions
    "login": os.environ.get("DOGFOOD_RATE_LOGIN", "10/m"),
    "invite_redeem": os.environ.get("DOGFOOD_RATE_INVITE_REDEEM", "20/h"),
    "submission_write": os.environ.get("DOGFOOD_RATE_SUBMISSION_WRITE", "60/h"),
    "ballot_write": os.environ.get("DOGFOOD_RATE_BALLOT_WRITE", "120/h"),
}
