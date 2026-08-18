# Lumina

AlmaLinux hardware certification catalog. Django + MariaDB + Valkey, OIDC via Keycloak.

See [the approved plan](../.claude/plans/glowing-launching-acorn.md) for the design
(stored outside the repo in the author's Claude state directory).

## Status

v1 scaffold complete, test-first throughout. 130+ tests covering every app.
Remaining v1 work is glue (OIDC realm import, production Keycloak config) and
the automated test-suite submission API (currently scaffolded at
`/api/v1/submissions/` with end-to-end auth; the create path itself returns 501).

## Docs

- [Disposable dev stack](docs/devstack.md)
- [Deployment (Ansible role)](docs/deployment.md)
- [Keycloak setup](docs/keycloak.md)
- [API reference](docs/api.md)
- [End-to-end manual test plan](docs/e2e.md)

## Dev quickstart (containers, no OIDC)

```bash
podman compose up --build -d
# Catalog: http://localhost:8000/
# Admin:   http://localhost:8000/admin/  (admin / admin)
# Review:  http://localhost:8000/review/ (reviewer / reviewer)
# Mail:    http://localhost:8025/
```

Details and options: [docs/devstack.md](docs/devstack.md).

## Dev on the host (if you'd rather not containerize)

```bash
# System prerequisites for the mysqlclient MariaDB driver:
sudo dnf install -y mariadb-connector-c-devel pkgconf-pkg-config gcc python3-devel

cp .env.example .env
podman compose up -d db valkey mailpit
pip install -e '.[dev]'
python manage.py migrate
python manage.py runserver
```

## Tests

```bash
pytest
```

That includes the tests that drive a real browser over the real pages, which need Chromium and
the extra:

```bash
sudo dnf install -y epel-release && sudo dnf install -y chromium
pip install -e '.[dev,browser]'
```

Without them the browser tests skip and everything else runs. They add about half a minute; see
[docs/browser-tests.md](docs/browser-tests.md) for what they cover and why they are not opt-in.

## Layout

- `lumina/settings/{base,dev,test,prod}.py` - split settings.
- `lumina/core` - landing page, shared helpers.
- `lumina/accounts` - OIDC backend + group sync, API tokens, user dashboard.
- `lumina/vendors` - Vendor and VendorMembership.
- `lumina/taxonomy` - admin-curated categories and their values (pending/approved/rejected).
- `lumina/hardware` - Systems, Components, Submissions, TestResultAttachments *(forthcoming)*.
- `lumina/audit` - audit log service *(forthcoming)*.
- `lumina/review` - in-app reviewer dashboard *(forthcoming)*.
- `lumina/api` - DRF read-only + future submission API *(forthcoming)*.
- `ansible/roles/lumina` - production deploy (gunicorn + nginx + systemd) *(forthcoming)*.
