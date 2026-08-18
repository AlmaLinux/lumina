# Disposable dev stack

Self-contained, throw-away container stack for local development and demos.
**No OIDC** - authenticates through the Django admin with a seeded
`admin`/`admin` superuser, so you can play with the app without a Keycloak
instance. Production deploys use the Ansible role, not this image.

## What you get

- `web` - Django (runserver, auto-reload) on **http://localhost:8000**
  - Catalog: http://localhost:8000/
  - Admin: http://localhost:8000/admin/ (login: `admin` / `admin`)
  - Review dashboard: http://localhost:8000/review/ (login: `reviewer` / `reviewer`)
  - JSON API: http://localhost:8000/api/v1/
- `db` - MariaDB 11 (tmpfs-backed; `docker compose down` wipes state)
- `valkey` - Valkey 8 (cache + sessions)
- `mailpit` - catches outgoing mail; web UI on **http://localhost:8025**

Sample taxonomy (Architecture, Certified for, Network, Management) and a
handful of vendors (Dell, Supermicro, Community-Submitted) are seeded on
first start.

## Spin up

```bash
podman compose up --build -d
# (or: docker compose up --build -d)
```

First run builds the AlmaLinux 10 base image and pulls MariaDB/Valkey/Mailpit.
Takes a couple of minutes. Subsequent runs are fast.

Watch logs while it boots:

```bash
podman compose logs -f web
```

The entrypoint waits for MariaDB, runs migrations, and seeds sample data
before handing control to `runserver`.

## Tear down

```bash
podman compose down -v
```

`-v` removes the `lumina-media` named volume too, so every `down -v` /
`up` cycle starts from a clean slate.

### If your database predates the migration squash

The migration history was squashed to a single initial state. That is what a
pre-release project wants for fresh dev copies, but it strands any database created
before the squash: its `django_migrations` table names migrations that no longer exist
on disk, so `migrate` neither replays nor upgrades it. It reports "No migrations to
apply" while actually being out of date, which is the unhelpful failure mode.

`down -v && up` fixes it and is the right move for a database holding only seeded
sample data. It is the wrong move if you have been uploading bundles from the CLI:
those runs and your activated API tokens live in the database, the bundles live in the
`lumina-media` volume, and `-v` removes both.

To repair in place instead, first check what the squashed migrations would build that
you do not already have:

```bash
podman compose exec web python manage.py makemigrations --check --dry-run
```

Then reconcile the ledger from a shell (`podman compose exec web python manage.py
shell`), which is safe precisely because the squash was generated from the finished
schema, so the tables already match:

```python
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

recorder, loader = MigrationRecorder(connection), MigrationLoader(connection)
on_disk = set(loader.disk_migrations)
for app, name in sorted(set(recorder.applied_migrations()) - on_disk):
    recorder.record_unapplied(app, name)      # names that no longer exist
for app, name in sorted(on_disk - set(recorder.applied_migrations())):
    recorder.record_applied(app, name)        # the squashed graph
```

Anything genuinely new since the squash still needs its tables built. Verify by
diffing `information_schema.columns` against a freshly migrated test database rather
than trusting the ledger, then create what is missing with
`connection.schema_editor()`. Confirm with `migrate` reporting no work, no `[ ]` rows
in `showmigrations`, and `makemigrations --check` clean.

## Iterating on code

The host working directory is bind-mounted read-write at `/app` inside
the `web` container (with `:z` for rootless podman + SELinux). `runserver`
auto-reloads on source changes - no image rebuild needed.

Rebuild only when dependencies in `pyproject.toml` change:

```bash
podman compose up --build -d
```

## Running tests

The devstack uses MariaDB, but the test settings use in-memory SQLite for
hermeticity - so you can run tests from inside or outside the container:

```bash
# Outside (recommended; faster):
python -m pytest

# Inside the container:
podman compose exec web python -m pytest
```

## Changing seed credentials

Override before bringing the stack up:

```bash
DEVSTACK_ADMIN_PASSWORD=hunter2 \
DEVSTACK_REVIEWER_PASSWORD=hunter2 \
  podman compose up --build -d
```

These are read by `lumina/core/management/commands/seed_devstack.py`.

## Troubleshooting

- **`/app` permission denied on first start** - ensure the bind mount
  still has `:z` in `compose.yaml`. Without it, rootless podman can't
  read the source under SELinux.
- **Port 8000/8025 already in use** - edit the `ports:` entries in
  `compose.yaml` or stop the conflicting service.
- **Rebuilding doesn't pick up a dep change** - `podman compose build
  --no-cache web` forces a fresh build.
