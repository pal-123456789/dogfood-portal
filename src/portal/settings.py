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
    # Read-only public API (/api/v1/): DRF + drf-spectacular OpenAPI 3 schema/docs. Self-hosted
    # Swagger UI assets via sidecar (no CDN). `api` has no models, so no migration is added.
    "rest_framework", "drf_spectacular", "drf_spectacular_sidecar", "api",
    # T3/T4 feature apps (additive; each owns its own tables/URL prefix and touches none of the
    # five flat checker routes). `voting` and `comments` ship models + a 0001 migration; `records`
    # and `embed` are model-free (records/embeds are computed on the fly from the live event graph).
    "voting", "comments", "records", "embed",
    # Wave-2 T4 operability apps (additive; own tables/URL prefix; touch none of the five flat
    # checker routes). `bundles` is model-free (a signed export is computed on the fly from the live
    # event graph); `webhooks` ships models + a 0001 migration for endpoints and recorded deliveries.
    "bundles", "webhooks",
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
    # T3 community voting: ballot casts (per campaign+voter) and project comments (per author).
    "vote_write": os.environ.get("DOGFOOD_RATE_VOTE_WRITE", "20/m"),
    "comment_write": os.environ.get("DOGFOOD_RATE_COMMENT_WRITE", "60/h"),
    # T4 outbound webhooks: endpoint registration (per event+organizer). webhooks.views reads this
    # via .get(default), so the key is optional at runtime; it is set here so the limit is explicit.
    "webhook_write": os.environ.get("DOGFOOD_RATE_WEBHOOK_WRITE", "60/h"),
}

# --- Read-only public API (/api/v1/) -------------------------------------------------------
# DRF is configured PUBLIC and read-only: no authentication classes and AllowAny, so there is no
# auth surface to get wrong and nothing here can expose a logged-in user's data. Responses are
# JSON only (the browsable API is off in every environment). PageNumberPagination sets PAGE_SIZE
# so `manage.py check --fail-level WARNING` (a build gate) stays warning-clean. The anon throttle
# FAILS OPEN (api/throttling.py) so a cache outage -- or the missing `dogfood_cache` table under
# `manage.py test` -- can never 500 the API.
REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": ["api.throttling.FailOpenAnonThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.environ.get("DOGFOOD_RATE_API", "240/min")},
}

SPECTACULAR_SETTINGS = {
    "TITLE": "DOGFOOD public API",
    "DESCRIPTION": (
        "Read-only public access to events, tracks, teams, public (SUBMITTED) project "
        "submissions, and official PUBLISHED results. Results are served verbatim from the "
        "signed, frozen normalization run -- never a live recompute -- and an unpublished event "
        "returns {\"published\": false} with no ranking rows. This API never exposes individual "
        "ballots, per-judge scores, judge identities, invitations, the audit chain, or any user "
        "PII (no emails or display names)."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # Self-hosted Swagger UI assets (no CDN), so the docs page honors the site's script-src 'self'.
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
}
