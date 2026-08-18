# The dev environment, and how CI keeps it current

The dev environment tracks the **`dev` branch**. Every merge to `dev` redeploys it, and only after CI
has passed on that same commit. `main` does not deploy anywhere.

Where each piece lives:

| Trigger | Where it is defined | Wipes the database |
|---|---|---|
| push to `dev` | the `deploy-dev` job in `ci.yml`, gated on the test jobs | **no** |
| by hand, any ref | `deploy-dev.yml` | **no.** It has no reset input |
| by hand, fresh database | `deploy-dev-reset.yml`, typed confirmation | yes, always |
| the deploy itself | `deploy-dev-run.yml`, called by all three | as told |

The automatic deploy is a job in `ci.yml` rather than a workflow of its own, and the reason is worth
knowing before you move it: a `workflow_run` trigger fires only from the copy of the workflow on the
repository's **default branch**. An automatic deploy defined in its own file on `dev` does not run for a
push to `dev` until that file reaches `main`, and it fails silently, leaving a green CI run and no
deploy with nothing anywhere to explain it. A job in `ci.yml` runs from the branch's own workflow file,
so it works the day you push it.

Because it is gated on `needs: [test, test-mariadb, lint-deploy]`, a red test job means no deploy. That
is the intended behaviour and it is also the first thing to check when a push to `dev` does not deploy.

What they all run is `ansible/playbooks/dev.yml`, the same `lumina` role production uses with a dev
host's answers substituted.



## What a dev host is

One AlmaLinux 9 or 10 machine, running everything itself:

| | dev | production |
|---|---|---|
| Settings | `lumina.settings.devstack` | `lumina.settings.prod` |
| Sign-in | Django password login, seeded accounts | Keycloak, OIDC |
| Database | MariaDB on the box, created by the play | separate host |
| Cache | Valkey on the box | separate host |
| TLS | on, self-signed at once and certbot behind it | nginx with a certificate |
| Mail | console backend, into the journal | a real relay |
| `DEBUG` | off | off |

`devstack` is what makes the box usable without a Keycloak realm: it drops the OIDC app and mounts a
real password form at the URL names `mozilla_django_oidc` would have published. Everything else is the
production path, including gunicorn on a unix socket, nginx in front, the `internal` media redirect,
and the two maintenance timers.

`DEBUG` is off deliberately. The box is reachable over the network, and a traceback page publishes
settings, the request, and local variables to anyone who can provoke one. The traceback is in
`journalctl -u lumina` either way. If you want it on for an afternoon, deploy with
`-e lumina_debug=true` from a laptop rather than changing the default.

## What the host needs before the first deploy

1. A user CI can reach over SSH, with passwordless `sudo`. `sudo` must be installed: Ansible
   escalates with it, and the play fails at fact-gathering without it.
2. Nothing else. The play installs MariaDB, Valkey, nginx, Python, and the application.

The secret key and the database password are generated **on the host** on first deploy and kept in
`/etc/lumina-secret-key` and `/etc/lumina-db-password`, mode 0600. They are not CI secrets: a value
generated on the box cannot leak through a workflow log, and regenerating the key on every deploy
would invalidate every session and signed URL on each merge.

## Repository configuration

Secrets, under Settings → Secrets and variables → Actions → Secrets. All four are required and the
workflow fails with a named error if one is empty:

| Secret | What it is |
|---|---|
| `DEV_SSH_HOST` | hostname or address of the dev box |
| `DEV_SSH_USER` | the SSH user (defaults to `deploy` if unset) |
| `DEV_SSH_KEY` | private key, the whole PEM including the header and footer lines |
| `DEV_KNOWN_HOSTS` | output of `ssh-keyscan -H <host>` |

`DEV_KNOWN_HOSTS` is not optional and not ceremony. Host key checking stays on, so a machine
answering on that address with a different key fails the deploy instead of receiving your key and your
code.

Variables, under the same page → Variables. Both optional:

| Variable | What it does |
|---|---|
| `DEV_URL` | shown on the deployment in the GitHub UI, and fetched after the deploy as an outside-in check |
| `DEV_HOSTNAME` | `server_name` for the vhost. Unset means `_`, which answers on any name or address |
| `DEV_CERTBOT_EMAIL` | **required for a trusted certificate.** The ACME account address. Unset means certbot is switched off entirely, the packages are not installed, and the host serves the self-signed certificate forever. Needs `DEV_HOSTNAME` too |
| `DEV_CERTBOT_STAGING` | `true` to use Let's Encrypt staging for a trial run. Certificates will be untrusted |

Create a GitHub environment named `dev` if you want required reviewers or a branch restriction on
deploys. The workflow already targets it, so nothing needs editing to add them.

### Signing in

Two mutually exclusive worlds, and which one you get is decided by whether `DEV_OIDC_ISSUER` is set:

| | `DEV_OIDC_ISSUER` unset | `DEV_OIDC_ISSUER` set |
|---|---|---|
| Login | seeded password form at `/oidc/authenticate/` | Keycloak, the production path |
| Users | created by `seed_devstack` | created on first login from the OIDC claims |
| Groups | set by `seed_devstack` | synced from the Keycloak `groups` claim on every login |

Nothing branches in a template to make that work. `lumina.settings.devstack` keeps
`mozilla_django_oidc` installed when a client id and an authorization endpoint are both present, and
`lumina/urls.py` mounts the password form only when that app is **absent**. So configuring a realm
removes the password form as a side effect, which is what you want: a public dev box should not keep a
password-authenticable superuser around once there is a real identity provider.

`/admin/login/` still accepts a password, because `ModelBackend` stays second in
`AUTHENTICATION_BACKENDS` exactly as it does in production. That is a break-glass route for the seeded
superuser, not a second front door, and if you want it gone it has to go from `base.py` for both.

#### Setting up the realm

The realm, the client, the groups, and the group-membership mapper are the same for every deployment
and are documented once in [keycloak.md](keycloak.md). Follow that, with two dev-specific notes:

- The **redirect URI** is `https://lumina.almalinux.dev/oidc/callback/`, trailing slash included. It
  is `mozilla_django_oidc`'s callback route and a mismatch fails at Keycloak with "Invalid parameter:
  redirect_uri" before Lumina sees anything.
- Use a **separate client** from production, so revoking dev's secret cannot affect the real site.

The step worth re-reading in that document is the groups claim. Keycloak sends no group membership by
default, and a missing claim is not an error anywhere: sign-in succeeds and the user has no permissions
at all, which reads as a Lumina bug and is not one. `keycloak.md` covers where the mapper can live and
how to confirm the claim is arriving.

#### Repository configuration

| Name | Kind | Value |
|---|---|---|
| `DEV_OIDC_ISSUER` | variable | `https://<keycloak>/realms/<realm>`, no trailing slash. The six `OIDC_OP_*` endpoints are derived from it |
| `DEV_OIDC_CLIENT_ID` | variable | the dev client's ID, e.g. `lumina-dev` |
| `DEV_OIDC_CLIENT_SECRET` | secret | from the Credentials tab |

Then Actions -> **Deploy dev** -> Run workflow. The deploy prints which world it is in:

```
login: Keycloak OIDC
```

#### When sign-in does not work

- **"Invalid parameter: redirect_uri"** at Keycloak. The client's redirect URI is not exactly
  `https://lumina.almalinux.dev/oidc/callback/`.
- **`invalid_scope`, "Invalid scopes: openid email profile groups"** on the redirect back. Something is
  asking for a `groups` scope the client does not have, and Keycloak refuses the whole request. There is
  no built-in scope by that name, and **creating the group-membership mapper does not create one**: a
  mapper adds a claim, a client scope is what makes the scope name legal to request. Lumina does not ask
  for it, so check `OIDC_RP_SCOPES` in `/etc/lumina.env` and `lumina_oidc_scopes`.
- **Signed in, but no permissions anywhere.** The `groups` claim is missing or empty. Check it at the
  userinfo endpoint with a fresh access token rather than guessing; `keycloak.md` has the two places
  the mapper can live and what each one implies.
- **A 500 on the callback.** Usually the JWKS endpoint being unreachable from the host, or a clock
  skew large enough to fail token validation. `journalctl -u lumina -n 100` has the traceback.
- **Back at the login page with no error.** `mozilla_django_oidc` failed to create the user. Almost
  always a missing `email` claim: the backend keys users on email, so the realm must release it.

### Accounts

`seed_devstack` falls back to `admin/admin` and `reviewer/reviewer` when nothing tells it otherwise. A
superuser with that password, on a box anyone can reach, is an open door to Django's admin, so the play
generates both on first deploy and keeps them in `/etc/lumina-dev-accounts.env` (mode 0600, root).
Read them off the host:

```
sudo cat /etc/lumina-dev-accounts.env
```

They are only applied when an account is created, which is what a reset is for. The file is never
regenerated, because a new value there would not reach an account that already exists and the file
would start lying.

### HTTPS

On from the first deploy, and it never waits for anything. Two certificate sources, used together:

1. A **self-signed** certificate is generated on the host if none exists, so nginx has something to
   serve immediately. Ten-year validity, a `subjectAltName` (current clients reject a certificate
   without one rather than merely distrusting it), private key `0600 root:root`, and never regenerated.
2. If `DEV_CERTBOT_EMAIL` is set, **certbot** then asks Let's Encrypt for a real one, and nginx switches
   to it the moment there is one.

**Only the first of those happens by default.** With `DEV_CERTBOT_EMAIL` unset, `lumina_certbot` is
false, so certbot is not installed and never runs, and the host serves the self-signed certificate
indefinitely. That is a configuration state and not a fault, but it looks exactly like a stuck deploy,
so both the deploy summary and the play say so: look for

```
TLS is on with a self-signed certificate and certbot is switched off
```

in the Ansible output. certbot needs an ACME account address and cannot invent one, which is why the
variable is required rather than defaulted.

certbot cannot hurt the deploy. It runs after nginx is already serving, it has a timeout, its failure
is reported rather than raised, and the site is on HTTPS either way. A certbot that never succeeds costs
nothing but trust.

It also cannot spam Let's Encrypt, and the rule is about *what* is on disk rather than whether
something is:

> The request is skipped only when `/etc/letsencrypt/live/<name>/fullchain.pem` holds a certificate
> that was issued by a CA and has not expired. Anything else there is not the certificate certbot was
> asked for, so it gets replaced: a **self-signed** certificate, a **staging** certificate once
> `DEV_CERTBOT_STAGING` is turned off, an **expired** one, or an unreadable one.

So a deploy on every merge never re-asks for a certificate it already has, and a real certificate is
never overwritten, while none of the placeholders can wedge the host into serving something untrusted
forever. The role decides by reading the certificate (`openssl x509 -issuer -subject -checkend 0`) and
its key: self-signed means issuer and subject are the same name, Let's Encrypt prefixes every staging
CA name with `(STAGING)`, and a certificate with no `privkey.pem` beside it is no more usable than a
missing one.

### Replacing something is not the same as asking for a certificate

Two facts about certbot shape the rest of this, and both cost a wasted issuance to learn:

1. **A lineage is `/etc/letsencrypt/renewal/<name>.conf`, not the files under `live/`.** certbot will not
   write into `live/<name>/` unless a lineage it can load is behind it, and it finds that out *after*
   the certificate has been issued. So a self-signed certificate sitting at that path, which is exactly
   the case this section is about, is cleared first: `certbot delete` when there is a lineage, otherwise
   the directory is moved to `<name>.displaced-<epoch>` and kept.
2. **`--cert-name` is a preference, not a guarantee.** When the renewal config survives but the lineage
   cannot be loaded (a pruned `archive/`, a restore that skipped it), certbot takes the new-request
   branch, collides with the surviving config, writes `<name>-0001`, and **exits 0**. Nothing at the path
   the role reads has changed.

`--force-renewal` therefore goes on only when the lineage is loadable, which is the one state certbot can
renew in place: the staging-to-production switch and an expired certificate. Without it certbot answers
"Certificate not yet due for renewal" and keeps what you wanted rid of.

And certbot's exit code is treated as a claim rather than as evidence. After a zero exit the role stats
and re-reads the certificate at its own path, and only a CA-issued, unexpired certificate *with its key*
counts as delivered. An exit 0 that did not deliver arms the back-off exactly like a failure, because
otherwise the marker is cleared and every merge buys another certificate against a weekly limit while
the log says it worked.

After a *failed* attempt it backs off for an hour (`lumina_certbot_retry_after`), because Let's
Encrypt allows five failed validations an hour per account and a busy branch can deploy more often
than that: without the back-off, one bad DNS record turns every merge into another failure against
that limit.

The self-signed certificate at `/etc/pki/tls/certs/<name>.crt` is never itself overwritten, and does
not need to be. It is a fallback, not a stage: nginx is pointed at whichever of the two is usable,
preferring the certbot one, so a real certificate supersedes it rather than replacing it. Regenerating
it on each deploy would only change the certificate under anybody who had accepted it. It *is*
regenerated when either half of the pair is missing, because half a pair serves nobody.

`nginx -t` runs before nginx is started and again after the vhost is re-rendered onto a new
certificate, and if the new one will not load, the vhost goes back to the self-signed pair and the
site stays up. nginx refuses to load a config naming a certificate or key it cannot read, and without
that check the refusal arrived as a failed handler at the very end of the deploy.

`certbot-renew.timer` is enabled only once there is a certificate to renew, and issuance records
`--deploy-hook "systemctl reload nginx"`, so the renewal in ninety days is picked up on its own. The
ACME challenge location sits **above** the redirect in the vhost, because a port-80 block that redirects
everything works today and breaks renewal three months from now.

`DEV_URL` should be `https://`; the outside-in check passes `curl -k`, since certificate trust is the
one thing it deliberately does not assert. `-e lumina_dev_tls=false` serves plain HTTP instead.

## Running it by hand

**Deploy, keeping the data.** Actions → **Deploy dev** → Run workflow. `ref` defaults to `dev`; set it
to any branch, tag, or SHA to put that on the box instead. The next merge to `dev` puts it back.

Tick **seed** to also run `seed_devstack`. It creates the demo listings and accounts if they are
missing and changes nothing that already exists, so it is safe to repeat and is what a new host needs
once. Seeding is off for merges because a merge should not create records.

**Retry certbot after a failed attempt.** Actions -> **Deploy dev** -> Run workflow, and tick
**retry_certbot**. A failed attempt leaves `/var/lib/lumina/certbot-last-failure` on the host and the
play then skips certbot for an hour, so an ordinary retry inside that hour does nothing and reads as the
deploy ignoring you. This ignores the marker for one run.

Before spending more attempts, find out why the last one failed. The reason is in the deploy log, on the
task named *Say so*, and in full on the host:

```
sudo tail -40 /var/log/letsencrypt/letsencrypt.log
```

Then try it without spending anything. `--dry-run` validates against the staging environment, writes no
certificate, and does not count against the production limits, so it answers "can Let's Encrypt reach
this host at all" for free:

```
sudo certbot certonly --dry-run --non-interactive --agree-tos \
  --webroot --webroot-path /var/lib/lumina/acme \
  --cert-name <name> --domain <name> --email <address>
```

`<name>` is `DEV_HOSTNAME`, and it has to be a name that resolves publicly to this box. The two things
that usually fail are outside the play's reach: a DNS record pointing somewhere else, and port 80 not
reachable from the internet. The role opens `http` in firewalld, but a cloud security group in front of
the instance is a separate thing it cannot see, and http-01 works by Let's Encrypt fetching
`http://<name>/.well-known/acme-challenge/...` over port 80. Check it from off the box:

```
curl -sS -o /dev/null -w '%{http_code}\n' http://<name>/.well-known/acme-challenge/probe
```

404 means the path is reachable and nginx is serving that location, which is what validation needs.
Anything that hangs or refuses is the firewall or DNS, not certbot.

**Deploy with a fresh database.** Actions → **Deploy dev (fresh database)** → Run workflow, and type
`wipe` in the confirmation box. That drops the database, deploys, migrates from empty, and reseeds.
Everything on the host goes: every run, every listing, every part-finished submission, and the demo
accounts come back with new passwords in `/etc/lumina-dev-accounts.env`.

The confirmation is checked in its own job, so a wrong answer stops before any secret is fetched and
before anything on the host is touched.

This is a separate workflow rather than a checkbox on the ordinary deploy for two reasons. A
destructive action should be something you go and find, not something next to the button you meant to
press. And keeping it out of `deploy-dev.yml` means the workflow that runs on every merge has no reset
input at all, so nothing misconfigured there can wipe the database.

## Nothing is listening on 80

Check in this order, because the first two answer it most of the time.

**Did a deploy actually run?** The automatic deploy is the `deploy-dev` job inside the **CI** run for
that push, not a separate workflow, so open the CI run and look for it after the three test jobs. If it
is missing, it was skipped: either the push was not to `dev`, or one of `test`, `test-mariadb`, or
`lint-deploy` failed. A red test job blocks the deploy by design.

**Did it run and pass?** Then nginx was listening when it finished, because the play's last step asks
the host's own nginx for a page and fails after ten tries if it does not answer 200. A pass means the
site answered on `127.0.0.1:80` on that host at that moment. So look at what changed since:
`systemctl status nginx lumina` and `journalctl -u nginx -n 50`.

**A 502 from nginx** means nginx is fine and gunicorn is not reachable on the unix socket. The deploy
now prints the reason itself: on a failed smoke check it dumps `systemctl status lumina`, the last 40
journal lines, gunicorn's error log, `ls -laZ` of the socket directory, nginx's error log, `getenforce`,
and any recent SELinux denials into the CI log. Read that before logging in.

Four causes look identical from outside, and the dump separates them: gunicorn not running at all, its
workers dying on import, nothing listening where nginx looks, or SELinux refusing the connection.

**nginx reaches gunicorn over TCP on the loopback**, not a unix socket, and that is the resolution of a
real failure rather than a preference. On an enforcing host, nginx as `httpd_t` connecting to a socket
held by an unconfined service needs `unix_stream_socket connectto`, which the targeted policy does not
grant and which no file label supplies. The observed symptom was nginx logging

```
connect() to unix:/run/lumina/gunicorn.sock failed (13: Permission denied)
```

with the socket mode `0777`, correctly labelled `httpd_var_run_t`, gunicorn up with four workers, and
**nothing at all in `ausearch`**, because that denial is `dontaudit`'d. Permissions and labels are the
first things anyone checks and both were already right. TCP needs only `httpd_can_network_connect`,
which the role sets, and which was in the role before anything used TCP.

`lumina_bind_tcp: false` goes back to the socket, on a host where you have added a policy module for
it; the fcontext labelling tasks only run in that mode.

**Listening but unreachable from outside** is the firewall, not nginx. `ss -ltnp | grep :80` on the
host tells you which of the two you have. The role opens `http` (and `https` when TLS is on) in
firewalld when firewalld is running, so this should be handled; `firewall-cmd --list-services` confirms
it. Set `lumina_manage_firewall: false` if something else manages the network.

## When a deploy fails

The play's last step asks the host's own nginx for a page and fails if it does not answer 200, so a
deploy that leaves a 502 fails rather than reporting success. Read it in this order:

1. The failing task name in the workflow log. Ansible names what it was doing.
2. `journalctl -u lumina -n 100` on the host. Application errors are here, not in the Ansible output,
   including any mail the console backend "sent".
3. `systemctl status lumina nginx mariadb valkey`.

The deploy is idempotent, so rerunning it is safe and is usually faster than reasoning about a
half-applied state. A deploy that changes nothing runs no handlers and does not restart the service.

## Running it from a laptop

```
cp ansible/inventory/dev.example.yml ansible/inventory/dev.yml   # then edit it
cd ansible
ansible-galaxy collection install -r requirements.yml -p collections
ansible-playbook playbooks/dev.yml
```

The `-p collections` is not optional. `ansible.cfg` sets `collections_path` to a project-local
directory so a run here uses the versions pinned in `requirements.yml` rather than whatever the
machine happens to carry. That is not tidiness: an open-ended floor let CI resolve `community.mysql`
5.x, which ships no modules at all and only redirects to `ansible.mysql`, so a lint against an older
system copy passed while CI failed. Both are bounded above now, and both CI and a laptop install into
the same place.

`dev.yml` is gitignored so a hostname does not end up in the repository. `ansible.cfg` sits beside the
playbooks, so a laptop run and a CI run behave the same.
