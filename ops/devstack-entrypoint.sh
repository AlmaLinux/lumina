#!/usr/bin/env bash
# Entrypoint for the disposable devstack container.
#
# - Waits for MariaDB to accept connections (compose healthchecks normally
#   handle this, but we belt-and-braces in case the health probe hasn't
#   caught up).
# - Runs migrations.
# - Seeds a superuser + sample taxonomy/vendor data so the stack is useful
#   immediately after ``docker compose up``.
# - Execs the command passed in (defaults to runserver from the Containerfile).

set -euo pipefail

: "${DB_HOST:=db}"
: "${DB_PORT:=3306}"

echo "[devstack] waiting for ${DB_HOST}:${DB_PORT}…"
until nc -z "${DB_HOST}" "${DB_PORT}"; do
    sleep 1
done

echo "[devstack] applying migrations"
python manage.py migrate --noinput

echo "[devstack] seeding superuser + sample data"
python manage.py seed_devstack

exec "$@"
