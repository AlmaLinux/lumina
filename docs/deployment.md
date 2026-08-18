# Deployment

Production: AlmaLinux host, gunicorn + nginx + systemd, MariaDB + Valkey external.
Not containerized.

## Integrating into an existing Ansible playbook

The role at [ansible/roles/lumina](../ansible/roles/lumina) is designed to slot into an existing
playbook:

```yaml
# site.yml
- hosts: lumina
  become: true
  roles:
    - role: lumina
      vars:
        lumina_hostname: catalog.almalinux.org
        lumina_secret_key: "{{ vault_lumina_secret_key }}"
        lumina_db_password: "{{ vault_lumina_db_password }}"
        lumina_oidc_client_id: lumina
        lumina_oidc_client_secret: "{{ vault_lumina_oidc_secret }}"
        lumina_oidc_issuer: https://keycloak.almalinux.org/realms/almalinux
        lumina_review_notify_emails: reviewers@almalinux.org
```

The sensitive variables (`lumina_secret_key`, `lumina_db_password`,
`lumina_oidc_client_secret`) should come from `ansible-vault`.

## Prerequisites on the target host

- AlmaLinux 9 or 10.
- Python 3.12 available (the role installs `python3.12` from base repos).
- MariaDB and Valkey reachable from the host (separate servers or
  co-located; the role does not install or manage them).
- TLS cert+key present at `lumina_tls_cert` / `lumina_tls_key`; certbot or
  an equivalent workflow should manage renewal.

## What the role does

1. Installs system packages (Python 3.12, mariadb-connector-c-devel, nginx,
   git).
2. Creates the `lumina` system user and directory layout under `/opt/lumina`
   and `/var/lib/lumina`.
3. Clones the app repo at `lumina_version` into `/opt/lumina/app`.
4. Creates a virtualenv at `/opt/lumina/venv` and installs the app.
5. Writes `/etc/lumina.env` with all Django settings.
6. Installs a hardened gunicorn systemd unit listening on
   `/run/lumina/gunicorn.sock`.
7. Installs an nginx vhost that terminates TLS and proxies to the socket.
8. Runs `migrate` and `collectstatic` on every deploy via handlers.

`migrate` is sufficient to bring up a usable database: `hardware/0003_reference_data`
seeds the CPU and GPU families every incoming run is classified against, plus the
three silicon vendors that own them. There is no separate seeding step for
production. That migration is idempotent (`get_or_create` on vendor and name) and
never overwrites an existing row, so a family whose patterns have been tuned in the
admin keeps them across deploys. `seed_devstack` is development-only sample data and
is not run here.

## Upgrading

Bumping `lumina_version` (e.g. to a tagged release) triggers the
`migrate`, `collectstatic`, and `restart` handlers. Rollback is just
setting `lumina_version` back to the prior tag and re-running.

## Manual deploy equivalents

If you want to deploy by hand once to verify the steps work, the
equivalent commands are:

```bash
sudo dnf install -y python3.12 python3.12-devel gcc git nginx \
  mariadb-connector-c-devel pkgconf-pkg-config
sudo useradd --system --home /opt/lumina --shell /sbin/nologin lumina
sudo install -d -o lumina -g lumina /opt/lumina/app /var/lib/lumina/media \
  /var/lib/lumina/static /var/log/lumina /run/lumina
sudo -u lumina git clone https://github.com/AlmaLinux/lumina.git /opt/lumina/app
sudo -u lumina python3.12 -m venv /opt/lumina/venv
sudo -u lumina /opt/lumina/venv/bin/pip install -e /opt/lumina/app
sudo -u lumina env $(cat /etc/lumina.env | xargs) \
  /opt/lumina/venv/bin/python /opt/lumina/app/manage.py migrate
```

Then enable `lumina.service` and reload nginx.
