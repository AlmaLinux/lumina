# Keycloak setup

Lumina uses Keycloak as its OIDC identity provider and relies on Keycloak
group membership for role assignment. Keycloak's underlying identity store
(FreeIPA/LDAP in the AlmaLinux deployment) is otherwise transparent to Lumina -
the one place it shows through is
[nested groups](#nested-freeipa-groups), where Keycloak and FreeIPA disagree
about what membership of a subgroup means.

## Realm

One realm per deployment - e.g. `almalinux`. No special realm settings are
required beyond the defaults.

## Client

Create an **OpenID Connect** client in the realm with:

- **Client ID**: `lumina` (matches `OIDC_RP_CLIENT_ID`)
- **Client authentication**: On (confidential client)
- **Standard flow**: Enabled (authorization code)
- **Valid redirect URIs**: `https://<lumina-hostname>/oidc/callback/`
- **Valid post-logout redirect URIs**: `https://<lumina-hostname>/`
- **Web origins**: `https://<lumina-hostname>`

Copy the generated client secret into `lumina_oidc_client_secret`
(ansible-vault). For the dev environment it goes in the `DEV_OIDC_CLIENT_SECRET`
GitHub Actions **secret** instead, alongside the `DEV_OIDC_ISSUER` and
`DEV_OIDC_CLIENT_ID` variables; see
[deploy-dev.md](deploy-dev.md#signing-in) for how that host switches between
Keycloak and its seeded password form.

## Groups

Lumina maps Keycloak groups to Django groups via
`settings.LUMINA_OIDC_GROUP_MAP`. The default map:

| Keycloak group         | Django group | What it grants                           |
|------------------------|--------------|------------------------------------------|
| `admins`               | `admin`      | Django superuser; admin+review access    |
| `almalinux-admins`     | `admin`      | the same, for a realm using the prefix   |
| `almalinux-reviewers`  | `reviewer`   | Access to `/review/` dashboard           |
| `almalinux-cert-sig`   | `certifier`  | May certify on AlmaLinux's behalf        |

It ships populated, so the AlmaLinux realm's `admins` group can sign in and
administer a fresh deployment with nothing configured. That matters more than it
looks: a missing entry is completely silent. Sign-in succeeds, the account has no
permissions anywhere, and no log line anywhere says why.

To use your own group names, set `LUMINA_OIDC_GROUP_MAP` in the environment
(Ansible: `lumina_oidc_group_map`), which **replaces** the default map rather
than extending it:

```
LUMINA_OIDC_GROUP_MAP=lumina-admins=admin,lumina-reviewers=reviewer
```

Replace and not merge, because every entry above is a grant and a deployment has
to be able to take one away. A realm with its own unrelated `admins` group would
otherwise hand every member of it `is_superuser` here, with no way to say no
short of editing `lumina/settings/base.py`. The Django group names that mean
something are `admin`, `reviewer`, and `certifier`; anything else is created and
carries no permissions.

Group **paths** are matched as well as names. Keycloak sends `admins`,
`/admins`, or `/almalinux/admins` depending on the mapper's "Full group path"
setting and whether the group is nested, and all three reach `admin`, because
which of them arrives is not something Lumina can see. The trade is that a group
named `admins` at any depth in the realm maps to Django's `admin`; if group names
are not meaningful across your whole realm, key the map on full paths instead.

`admins` grants `is_staff` and `is_superuser` on login, so membership of that
group in the realm *is* superuser access to the deployment.

Users not in any mapped group log in as plain authenticated users - they
can submit listings at community-validated trust only.

## Nested FreeIPA groups

FreeIPA expresses "the admins are Lumina admins" by putting the `admins` group
*inside* `lumina-admins`. Somebody whose only direct membership is `admins` is,
in FreeIPA's terms, a member of `lumina-admins` too - and that is the membership
you want to key the Lumina map on, so the app's roles are named by groups that
exist for the app.

Getting that as far as the `groups` claim is the whole problem, because it is
where the two systems disagree. **Keycloak does not propagate membership from a
subgroup up to its parent.** A Keycloak subgroup inherits its parent's *role
mappings* and attributes; its members are not members of the parent. So a claim
built from Keycloak's own group model names the child and never mentions the
parent.

There are two ways to close the gap, and they are not exclusive - the second
works whichever way the first is set, so configuring nothing in Keycloak is a
valid choice here.

### Flatten it in Keycloak (preferred)

FreeIPA's 389-ds `memberof` plugin already computes indirect memberships: a user
in `admins` has **both** `cn=admins,...` and `cn=lumina-admins,...` in their
`memberOf` attribute, because the plugin follows group nesting. Reading groups
from that attribute therefore gets the flattening for free.

On the LDAP user federation's **group-ldap-mapper**:

- **User Groups Retrieve Strategy**: `GET_GROUPS_FROM_USER_MEMBEROF_ATTRIBUTE`
- **Member-Of LDAP Attribute**: `memberOf`
- **Preserve Group Inheritance**: **Off**

The last one is not a preference: Keycloak rejects the combination of preserved
inheritance and the `memberOf` strategy, since a flat attribute cannot describe a
hierarchy. With inheritance off, groups arrive flat, so `admins` and
`lumina-admins` appear side by side in the claim and a map keyed on
`lumina-admins` matches directly.

Do **not** reach for `LOAD_GROUPS_BY_MEMBER_ATTRIBUTE_RECURSIVELY` for this. It
is implemented with `LDAP_MATCHING_RULE_IN_CHAIN`
(`1.2.840.113556.1.4.1941`), an Active Directory extension that 389-ds does not
implement, so against FreeIPA it silently returns nothing rather than failing.

Verify at the userinfo endpoint, as with everything else here - the claim should
list the parent:

```json
"groups": ["admins", "lumina-admins"]
```

### Let Lumina walk up the path

With **Preserve Group Inheritance on**, Keycloak imports the FreeIPA nesting as a
real hierarchy and the claim reads `["/lumina-admins/admins"]`. Lumina matches a
nested group against its ancestors as well as itself, so that grants whatever
`lumina-admins` maps to:

| Claim | Map key that matches |
|---|---|
| `/lumina-admins/admins` | `lumina-admins`, `admins`, `lumina-admins/admins` |
| `/almalinux/sysadmins/admins` | `almalinux`, `sysadmins`, `admins`, and the two full ancestor paths |

The walk is upward only, so `/other-app/admins` does not reach a mapping keyed on
`lumina-admins`. Each ancestor is matched by full path and by bare name, the same
two ways the group itself is.

This is on by default, because upward membership is the meaning nesting has in
the directory that is the source of truth here, and because the failure without
it is the silent one: sign-in succeeds and grants nothing. Set
`LUMINA_OIDC_GROUP_NESTED_PARENTS=false` (Ansible:
`lumina_oidc_group_nested_parents`) for a realm that uses Keycloak-native
subgroups to *narrow* a parent group rather than to feed it, where treating a
child as its parent would over-grant. The rule lives in
`lumina.accounts.auth.claimed_group_keys`.

## Who may claim AlmaLinux validation

`certifier` exists so that certifying on AlmaLinux's behalf is separable from
administering the application. `admin` also grants it, but that group is
escalated to `is_staff`/`is_superuser` on login, so using it for this would
mean every Certification SIG member had to be a superuser of the whole
deployment. Certification authority and infrastructure authority are different
jobs and now have different switches.

Trust levels a submitter may claim, from `derive_allowed_levels`:

| Who | May claim |
|---|---|
| Any authenticated user | `community` |
| Submit-role member of a **verified** vendor, for that vendor's hardware | `community`, `vendor` |
| `certifier` or `admin` | `community`, `vendor`, `almalinux` |

A claim is always capped at what the submitter is entitled to, so posting a
higher level than allowed silently yields the highest permitted one rather than
being rejected. Vendor and AlmaLinux validation rank equally.

## Groups claim

Keycloak does not include group memberships in the ID token by default.
Add a **Group Membership** mapper on the client:

- **Name**: `groups`
- **Token Claim Name**: `groups`
- **Full group path**: OFF (Lumina strips the leading `/` either way, but
  plain names are easier to match)
- **Add to ID token**: ON
- **Add to userinfo**: ON

After saving, confirm by hitting the userinfo endpoint with a fresh access
token - you should see `"groups": ["almalinux-reviewers", ...]`.

## Scopes

`openid email profile groups` - listed in `OIDC_RP_SCOPES` in Lumina's
settings.

**A mapper is not a scope.** These are two different objects and creating one
does nothing for the other. The mapper adds the `groups` *claim* to a token; a
client scope named `groups` is what makes `scope=groups` a legal thing to ask
for. Keycloak validates every requested scope against the client's assigned
client scopes and rejects the whole authorization request if one is unknown,
redirecting straight back with

```
error=invalid_scope&error_description=Invalid+scopes:+openid+email+profile+groups
```

so nobody signs in at all. There is **no built-in client scope named `groups`**.
For reference, what a freshly created client has (Keycloak 26):

| | scopes |
|---|---|
| Default | `acr`, `basic`, `email`, `profile`, `roles`, `web-origins` |
| Optional | `address`, `microprofile-jwt`, `offline_access`, `organization`, `phone` |

`openid` is not in either list and never needs to be: it is the OIDC marker
scope and is always accepted.

Lumina therefore asks for `openid email profile` and **not** for `groups`, which
is why the mapper alone is enough. Verified against Keycloak 26: with the mapper
on the client's dedicated scope and no `groups` client scope in the realm, the
claim is present in both the access and ID tokens for a request asking only for
`openid`. If your realm does have a `groups` client scope assigned to the client,
you may ask for it by setting `OIDC_RP_SCOPES` (Ansible: `lumina_oidc_scopes`),
but there is no reason to.

Two places the mapper can live, and they behave differently. Either is fine;
mixing up which one you used is what makes this hard to debug:

- **On the client's own dedicated scope** (`<client>-dedicated`, which is what
  "add a mapper on the client" means in the admin console). The claim is then
  always included, whatever scopes the request asks for. Simplest, and what the
  section above describes.
- **On a separate client scope** named `groups`. Then it is included only when
  that scope reaches the token, so the scope has to be assigned to the client
  under Client scopes, and **Default** is the assignment to use: Optional applies
  it only when the request asks for it, and Lumina does not ask by default.

Either way, verify rather than assume: hit the userinfo endpoint with a fresh
access token and look for `"groups"`. A missing claim is not an error anywhere.
Sign-in succeeds and the user simply has no permissions, which reads as a Lumina
bug.

## Local development

The `compose.yaml` in the repo starts a dev Keycloak on :8080 with an
admin/admin account. Create the `almalinux` realm and `lumina` client by
hand or import a realm JSON (`docs/keycloak-realm/` - placeholder for a
future export).
