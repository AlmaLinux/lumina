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
        # Optional. Replaces the shipped Keycloak group -> Django group map; see
        # docs/keycloak.md#groups.
        lumina_oidc_group_map: "lumina-admins=admin,lumina-reviewers=reviewer"
        lumina_review_notify_emails: reviewers@almalinux.org
        lumina_mattermost_webhook_url: "{{ vault_lumina_mattermost_webhook_url }}"
```

The sensitive variables (`lumina_secret_key`, `lumina_db_password`,
`lumina_oidc_client_secret`, `lumina_mattermost_webhook_url`) should come from `ansible-vault`.

## Chat notifications

Notification endpoints and their routing are rows an admin owns, which is right: a reviewer can add
a chat room without a deploy. It also means they live only in the database, so a restore that is
not a full dump comes back with no chat notifications and nobody notices until somebody asks why
the queue has gone quiet.

`lumina_mattermost_webhook_url` is the answer to that. Set it, and every deploy runs
`manage.py configure_notifications`, which makes sure the Mattermost endpoint exists with that URL
and that every event a reviewer has to act on posts to it. It is idempotent, so rotating the
webhook is a new value and a redeploy.

Anything else in the notifications tables belongs to whoever created it. The reconcile keeps its
own endpoint's URL in step, adds the reviewer routes, and touches nothing else - including
`enabled`, so silencing a noisy endpoint or one of its rooms in the admin survives a deploy.
Personal events are never posted: a channel is a room, and "your run was rejected" is for its
submitter, who is mailed as before.

Leave the variable blank and nothing is configured, which is also how an installation says it has
no chat at all.

The URL is the credential - anyone holding it can post into the channel - so keep it out of an
inventory. Anywhere Ansible can produce a value works, including a lookup against a secret store:

```yaml
lumina_mattermost_webhook_url: >-
  {{ lookup('community.hashi_vault.vault_kv2_get', 'lumina/mattermost').secret.webhook_url }}
```

That collection is not a dependency of this role; only the variable is. Use `ansible-vault`, an
environment variable, or whatever the surrounding playbook already uses for its other secrets.

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
8. Runs `migrate`, `configure_notifications`, and `collectstatic` on every deploy via
   handlers. `seed_certified_systems` is **not** among them - see below.

`migrate` is sufficient to bring up a usable database: `hardware/0003_reference_data`
seeds the CPU and GPU families every incoming run is classified against, plus the
three silicon vendors that own them. There is no separate seeding step for
production. That migration is idempotent (`get_or_create` on vendor and name) and
never overwrites an existing row, so a family whose patterns have been tuned in the
admin keeps them across deploys. `seed_devstack` is development-only sample data and
is not run here.

## Email

The role's defaults are a local MTA on the loopback: no login, no TLS, port 25. A host with its
own postfix needs nothing set.

For Amazon SES, or any hosted submission service:

```yaml
lumina_email_host: email-smtp.us-east-1.amazonaws.com
lumina_email_port: 587
lumina_email_use_tls: true
lumina_email_host_user: "{{ vault_lumina_email_host_user }}"
lumina_email_host_password: "{{ vault_lumina_email_host_password }}"
lumina_default_from_email: certification@almalinux.org
```

Three things SES will otherwise refuse the message for:

- **The credentials are SES SMTP credentials**, generated in the SES console. They are not AWS
  access keys, and an access key pasted here fails authentication. The password belongs in
  `ansible-vault`.
- **`lumina_default_from_email` has to be an identity SES has verified** - the address or its
  domain. An unverified sender is rejected for every message, not just the first.
- **TLS is required on both submission ports.** Use `lumina_email_use_tls` for 587 (STARTTLS) or
  `lumina_email_use_ssl` for 465 (implicit TLS), never both: Django refuses the pair, and it
  refuses it when it connects rather than at startup.

That last one is why the role runs `manage.py check`. Nothing here sends mail inside a request -
every message goes through the `deliver_notifications` timer with a retry ladder behind it - so a
relay that will not accept the connection breaks no page and raises into nobody's browser. The
drainer records the error, backs off five times, marks the delivery failed, and mail silently
stops. The system checks turn the four ways of getting this wrong into a failed deploy instead;
`lumina.E001` through `lumina.W003` each name what to change.

## The certifications the catalog launches with

`migrate` brings up a usable but empty catalog. The four systems published at
[almalinux.org/certification/ecosystem-catalog](https://almalinux.org/certification/ecosystem-catalog)
are recorded by a command, run once on a new install:

```bash
sudo -u lumina bash -c 'set -a && . /etc/lumina.env && set +a && \
  /opt/lumina/venv/bin/python /opt/lumina/app/manage.py seed_certified_systems --dry-run'
```

Drop `--dry-run` to apply. It only ever adds whole listings, so running it twice records
nothing and a listing whose tier somebody has since corrected is left alone. A listing deleted
by hand does come back on the next run unless it is also removed from the command's table.

Deliberately not a migration: four published listings in every database broke sixty-odd tests
that counted the catalog or resolved a vendor that was not meant to exist yet. Releases are
infrastructure and migrate (`releases/0002_seed_releases`); catalog content is a decision, and
is run when somebody makes it.

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
