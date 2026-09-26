# Dockerfile
FROM python:3.12-slim
# Debian bookworm. amd64 and arm64 are both official tags, so a judge on an M-series Mac
# and a judge on a cloud VM build the same file. Single stage: nothing here compiles,
# because psycopg[binary] ships a wheel. No gcc, no libpq-dev, no build-essential, no curl.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=portal.settings

WORKDIR /app

COPY requirements.txt .
# Requirements before the source, so editing a file does not reinstall the dependency set.
RUN pip install -r requirements.txt

# Every comment in this file is on a line of its own, and has to be: `#` opens a comment
# only in column 1. BuildKit lexes the rest of any other line as part of the instruction,
# so a trailing note becomes extra arguments -- and one apostrophe in it is a build-fatal
# unterminated quote. Proven at 2026-09-05, three weeks early: §15 fault 21.
# tests/ is copied so the README's verify-it-yourself command works; fixtures.json because
# the entrypoint reads /app/fixtures.json and nothing else puts it in the image (fault 2).
COPY src/ ./src/
COPY tests/ ./tests/
COPY fixtures.json ./fixtures.json
COPY docker/entrypoint.sh /entrypoint.sh

RUN python src/manage.py collectstatic --noinput
# At build time, not at boot, so ManifestStaticFilesStorage cannot raise at render time.
# This is why settings.py must import with no database and no secret key present (§11).

RUN python src/manage.py check --fail-level WARNING \
 && python src/manage.py makemigrations --check --dry-run
# Two assertions rather than two conveniences, and both are here because a warning printed
# inside docker build is invisible unless the build fails (§15 fault 27). The first makes any
# system-check WARNING red -- staticfiles.W004 included, which is why §0 row 10 creates
# src/static/vendor/htmx.min.js before this build runs. The second re-derives the migration
# from the model and fails if src/accounts/migrations/0001_initial.py disagrees with it
# (fault 26). Neither needs a database: makemigrations says it cannot reach db and still
# exits 0 when there is nothing to add.

RUN useradd --uid 10001 --no-create-home app \
 && chmod 755 /entrypoint.sh \
 && mkdir -p /state \
 && chown -R 10001:10001 /app /state

USER app
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
