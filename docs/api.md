# API reference

Lumina exposes a REST/JSON API under `/api/v1/`. All read endpoints are
anonymous-accessible and page-paginated (25 per page).

## Listings

### `GET /api/v1/systems/`
### `GET /api/v1/components/`

Query parameters:

- `vendor=<slug>` (repeatable) - filter by vendor; OR within.
- `<category-slug>=<value-slug>` (repeatable) - filter by taxonomy value;
  OR within a category, AND across categories.
- `page=<n>` - standard DRF pagination.

Example:

```
GET /api/v1/systems/?vendor=dell&architecture=x86_64&architecture=aarch64
```

### `GET /api/v1/systems/<slug>/`
### `GET /api/v1/components/<slug>/`

Single listing by slug.

Both kinds carry `compatibility`: one entry per AlmaLinux release the listing is
certified on, each with its **own** tier and its own evidence count. Newest first.

```json
"validation_level": "vendor", "validation_level_display": "Vendor-validated",
"attestation_count": 14,
"compatibility": [
  {"major": 10, "display": "AlmaLinux 10",
   "validation_level": "community",
   "validation_level_display": "Community-validated",
   "source": "run", "certifications": [],
   "community_confirmations": 3, "attestation_count": 3},
  {"major": 9, "display": "AlmaLinux 9",
   "validation_level": "", "validation_level_display": "",
   "source": "declared", "certifications": [],
   "community_confirmations": 0, "attestation_count": 0},
  {"major": 8, "display": "AlmaLinux 8",
   "validation_level": "vendor", "validation_level_display": "Vendor-validated",
   "source": "run", "certifications": ["vendor"],
   "community_confirmations": 10, "attestation_count": 11}
]
```

**Read this list rather than the top-level badge** if you care whether a machine is
still being validated. The listing's `validation_level` is the highest across all
releases and its `attestation_count` their total, so the example above reads
`vendor` on the strength of AlmaLinux 8 alone while the only evidence on 10 is from
the community. That is the abandonment case made visible.

Majors only, the same unit the software catalog uses. Hardware rows carried a
`minimum_minor` floor until 2026-08 and published labels like `"AlmaLinux 9.4+"`;
that field is **removed** from this payload. The minor a run passed on is still
available per run, where it is provenance for the evidence rather than the scope of
the claim.

`source` is `run` when an approved validation run proved the release, or `declared`
when support was stated with nothing behind it yet, and **it is the only field that
tells you which**. A declared release may carry a `validation_level`: accepting a
manual submission records a community attestation, so the release reads `community`
while nothing has actually been run on it. Read the two fields together. A client
treating a non-empty `validation_level` as "verified" will be wrong about every
declared listing.

**`certifications` and `community_confirmations` are the two halves of the
evidence, and they answer different questions.** `certifications` lists who
*asserted* the certification - `vendor`, `almalinux`, or both, since a release can
hold both. `community_confirmations` counts everyone else who ran the suite and
agreed. Hardware stores both in one table distinguished by tier, so reading only
the total would credit a vendor with the community's runs: in the AlmaLinux 8 row
above the vendor certified it and ten other people confirmed it.

`attestation_count` is every attestation on the release, official and community
together, and keeps that meaning. Software's per-major `attestation_count` is
community-only, because software keeps its certifications in a separate table.

All three counts are one per person per release: a repeat run by the same submitter
on the same release does not add to them, while that same person validating a
different release does.

System listings carry `cpu_support`: the CPU families the machine relates to,
each with its provenance. Most servers accept more than one generation, and a
validated generation says nothing about the others, so the two are
distinguishable:

```json
"cpu_support": [
  {"name": "Intel Xeon Scalable 2nd Generation", "vendor": "Intel",
   "slug": "intel-xeon-scalable-2nd-generation",
   "validated": true, "validation_level": "almalinux"},
  {"name": "Intel Xeon Scalable 1st Generation", "vendor": "Intel",
   "slug": "intel-xeon-scalable-1st-generation",
   "validated": false, "validation_level": null}
]
```

`validated: true` means an approved, passing validation run proved that family
on this system, and `validation_level` says by whom. `validated: false` is the
vendor's own statement of support with no certification behind it. Validated
entries come first.

Component listings carry the reverse view, `used_in_systems`:

```json
"used_in_systems": [
  {"name": "PowerEdge R760", "vendor": "Dell Inc.", "slug": "dell-poweredge-r760",
   "relation": "validated", "validation_level": "almalinux"}
]
```

`relation` is `validated` (a passing run proved this CPU family in that
system), `supported` (the vendor states the system accepts it), or `present`
(the part was found inside that system, which is how motherboards and GPUs
relate). The list rolls up through the family/model pair in both directions, so
a specific CPU model reports the systems certified for its family. Only
published systems appear, so embargoed hardware is never revealed here.

## Software

### `GET /api/v1/software/`

Published software products. Filters mirror the browse page exactly, because both
call the same `filter_software`:

| Parameter | Repeatable | Meaning |
|---|---|---|
| `q` | no | Name, publisher name, or description |
| `vendor` | yes | Publisher slug; OR within |
| `alma` | yes | AlmaLinux **major**; OR within. Matches approved releases only |
| `<category-slug>` | yes | OR within a category, AND across categories |

Software is certified per AlmaLinux **major**. There is no minor floor and no
tracking of the vendor's own product version numbers - unlike hardware, where a
run proves a specific `9.6` and the minor is evidence.

Software listings carry no licensing information. Whether a product is open source,
commercial, or both is a fact about its business model rather than about AlmaLinux
compatibility, and its own vendor publishes it better than this catalog can.

### `GET /api/v1/software/<slug>/`

```json
{"slug": "vaultwise-vaultwise-archive", "name": "Vaultwise Archive",
 "vendor": {"slug": "vaultwise", "name": "Vaultwise", "verified": true},
 "validation_level": "vendor", "validation_level_display": "Vendor-validated",
 "compatibility": [
   {"major": 9, "validation_level": "vendor",
    "validation_level_display": "Vendor-validated",
    "certifications": ["vendor", "almalinux"], "attestation_count": 12},
   {"major": 10, "validation_level": "community",
    "validation_level_display": "Community-validated",
    "certifications": [], "attestation_count": 3}]}
```

`compatibility` is the part worth reading. **Each AlmaLinux major carries its own
validation level and its own community confirmation count**, so a product
certified on 9 but only community-backed on 10 is distinguishable from one
certified on both. A vendor who certifies once and stops maintaining a listing
cannot hide behind a single badge.

`certifications` may contain **both** `vendor` and `almalinux` for the same major,
and both stay listed. `validation_level` reports the higher of the two, and the
tiers are ordered `community` < `almalinux` < `vendor`: the vendor is who has to
keep supporting the product, so their own certification is the stronger statement,
while AlmaLinux certifying it as well is a third party vouching. `attestation_count`
counts independent community members, capped at one per person per major, and is
shown alongside a vendor or AlmaLinux certification rather than replaced by it.

`validation_level` at the top level is the **highest** tier across all of the
product's releases. Read `compatibility` for the release you actually care about.

Community members can report that a product works on a release its vendor has not
cited. Those reports are held for review and **never appear here** until a
reviewer accepts them.

## Taxonomy

### `GET /api/v1/categories/`

All admin-curated categories plus their **approved** values. Useful for
building a client-side filter UI. `applies_to` is one of `system`, `component`,
`both` (meaning systems and components), or `software` - the software vocabulary is
separate, because Backup and Analytics have nothing to do with Architecture and
PCIe Generation.

### `GET /api/v1/vendors/`

All vendors. `verified=true` means the vendor is eligible to have
vendor-validated submissions accepted on its behalf. Vendors are scoped to a
catalog (`hardware`, `software`, or `both`), since the two populations barely
overlap; memberships, aliases, and verification are shared regardless.

## Submissions

### `POST /api/v1/submissions/`

**Status: scaffolded in v1.** Returns `501 Not Implemented` once auth is
validated. The auth path is fully working and exists so the test-suite
CLI client can be built against a real endpoint:

- **Authentication**: `Authorization: Bearer <token>` where `<token>` is
  issued via `/accounts/tokens/` (coming in a later increment) or the
  Django admin.
- **Required scope**: `submit`. A read-only token gets 403.

When implemented, the create payload will be multipart form data:

- `kind`: `system` or `component`
- `name`, `model_number`, `vendor`, `description`: listing fields
- `claimed_validation_level`: `community`, `vendor`, or `almalinux`

A software submission uses the same three values. The claim is always capped at
what the submitter is entitled to by `derive_allowed_levels`, and on approval the
reviewer's final level is applied to **every** AlmaLinux major the submission
cites. At the community tier there is nothing to certify, so the submitter's own
confirmation is recorded instead - which is why a freshly published community
listing shows a count of one rather than zero.
- `on_behalf_of`: optional vendor slug
- `attachments[]`: one or more test-result files
- `propose__<category-slug>`: optional new category values

## Certification-suite results

The alma-cert suite's report schema lives in the suite repository
(`docs/schema.md` there); Lumina validates `schema_version: "1.0"`.

### `POST /api/v1/results/`

Ingest a result bundle (the `.tar.zst` produced by `alma-cert bundle`, and
what `alma-cert submit` POSTs). Compression is sniffed from magic bytes:
zstd is the current format, gzip bundles from older suite versions are
still accepted.

- **Authentication**: session, or `Authorization: Bearer <token>` with the
  `submit` scope.
- **Body**: multipart form data - `bundle` (the tarball), optional
  `pre_release` (`true`/`false`) and `publish_after` (`YYYY-MM-DD`)
  overriding the report's own values, optional `notes` for the reviewer.
- **Throttle scope**: `results-ingest` (default 30/hour).
- **Responses**:
  - `201` `{uuid, status, run_type, claim_scope, web_url}` - accepted, pending review.
    `claim_scope` echoes what the server understood the run to be a claim about, so a
    submitter who passed `--scope` can see it was recorded rather than discovering
    otherwise from the review page.
  - `200` `{uuid, duplicate: true, web_url}` - byte-identical replay
  - `409` - same run UUID, different content
  - `400` `{code, detail}` - `bad_archive`, `unsupported_schema`,
    `invalid_report`, `manifest_mismatch`, `missing_bundle`
  - `413` - bundle exceeds `LUMINA_BUNDLE_MAX_BYTES`

The manual upload form at `/results/upload/` goes through the identical
ingest service.

#### Runs not performed on AlmaLinux

A bundle whose `environment.os.id` is not `almalinux` is **accepted and
quarantined**, not rejected: the submission stays visible to reviewers instead of
being silently bounced. The response is still `201`, with
`status: "quarantined"`.

`ID` is compared exactly, case-insensitively. `ID_LIKE` is not consulted - every
RHEL rebuild carries `ID_LIKE="rhel centos fedora"`, so honoring it would admit
precisely the distributions this check separates. An absent or empty `os.id` is
quarantined too, because "cannot tell" must not mean "supported".

A quarantined run:

- is **never public** - `public()` requires `approved`, so it appears in no feed,
  leaderboard, listing, or read endpoint;
- has **no `alma_release`**, even though a rebuild's `version_id` would parse
  identically to AlmaLinux's (Rocky 9.6 and AlmaLinux 9.6 both read as 9.6);
- **cannot be approved** - it is not an open status, and `_require_supported_os`
  re-checks the reported OS independently, so editing the status by hand does not
  get around it; and
- **certifies nothing** - no `ListingVersion`, no `CommunityAttestation`.

A reviewer who can see the report is wrong about the OS (a rebuilt or minimised
image, a container inheriting its base image's `/etc/os-release`) releases it with
a required reason, logged as `test_run.quarantine_release`. The run then rejoins
the normal queue and is reviewed on its merits; the release also binds the
AlmaLinux release that ingest refused to resolve. Rejecting is the other outcome.

alma-cert refuses to upload such a run in the first place, so anything reaching
here is an older client, a modified one, or a hand-built bundle.

### `GET /api/v1/results/` and `GET /api/v1/results/<uuid>/`

Public (approved, non-embargoed) runs. Filters: `run_type`, `alma`, `cpu`,
`gpu`, `gpu_driver`, `vendor`, `system=<listing-slug>`. The detail view
includes per-test results, benchmark metrics, and the inventory snapshot.

Every run carries what it is a claim **about**, which is not the same as the hardware it
reports:

- `claim_scope` - the component kinds this run certifies, or `[]` for a whole machine. `gpu`
  is the only kind emitted today. A run with a non-empty scope has no `system` and can never
  gain one, and it asserts nothing about the machine its `system_vendor` and `system_product`
  name, which are the host it was measured in.
- `claim_subject` - that component in words, for example `"UHD Graphics 630"`, or `""` for a
  whole-machine run.

Read both before attributing a `verdict`. `system: null` alone does not distinguish a
component claim from a machine run that is not yet linked to a listing.

The detail view also carries the CPU's feature flags, lifted out of the inventory
so a consumer asking "does this machine have `avx512f`" need not know they live at
`inventory.summary.cpus[0].flags`:

- `cpu_flags` - the full advertised set, a sorted de-duplicated list of lowercase
  names. Sorted by the suite, so two runs of one CPU are byte-identical and two
  machines are diffable. `[]` for a run from a suite that did not collect them.
- `cpu_flag_groups` - the notable entries grouped by what they tell you
  (`virtualization`, `confidential_computing`, `crypto_acceleration`,
  `vector_extensions`, `speculation_controls`, `timing_and_scheduling`). Groups
  with nothing present are omitted rather than returned empty: these are
  capabilities, and a CPU is not defective for lacking AMX. Taken from the
  informational `validate.cpu.flags` result, so it is `{}` on a `collect`-only run
  or one predating that test - `cpu_flags` is still populated in that case.

**Both are detail-only.** A current x86 CPU advertises 150-200 flags, so repeating
that per row would multiply a page of list results several times over.
Submitters and reviewers can fetch their own non-public runs when
authenticated; everyone else gets a plain 404 (embargoed runs are never
acknowledged).

### `GET /api/v1/benchmarks/`

Benchmarks with public results: `benchmark_id`, `category`, run counts,
latest version.

### `GET /api/v1/benchmarks/<id>/leaderboard/`

Ranked public results for one `(benchmark, version, metric)` tuple. Params:
`metric` (default: the suite-declared primary), `version` (default: newest
with public data), plus the hardware filters above.

## Device authorization (test-suite bootstrap)

RFC 8628-shaped flow used by `alma-cert register`, so the headless machine
under test never sees a password:

### `POST /api/v1/device/code`

Body `{client_name}` → `{device_code, user_code, verification_uri,
expires_in, interval}`. Anonymous, heavily throttled, max 3 pending
requests per IP.

### `POST /api/v1/device/token`

Body `{device_code}`. Poll every `interval` seconds. Errors follow
RFC 8628: `authorization_pending`, `slow_down`, `expired_token`,
`access_denied`. On approval the first successful poll returns
`{token, expires_at, scopes: ["submit"]}` - the token is minted lazily at
that moment and the raw value is never stored nor shown again.

The operator approves at `/my/activate/` (shows client name and requesting
IP before confirming). Tokens are short-lived
(`LUMINA_CLI_TOKEN_TTL_SECONDS`, default 12 hours) and manageable at
`/my/tokens/`.

### `GET /api/v1/token`

Confirms a stored token still works, and says what it can do:
`{valid, username, scopes, expires_at, hostname}`. Bearer token only - a
browser session is refused, because the question is about the token.

Callable by **any** scope, read-only included: its job is answering "is this
credential real", so gating it behind `submit` would leave a read token unable
to discover that it is a read token. Nothing secret is returned; the caller
already holds the token, and no part of the token itself is echoed back.

`401` for anything unknown, revoked, expired, or absent. Nothing else means the
token is bad - a `404` from an older server or a `502` from a proxy says only
that nobody could be asked.

`alma-cert` calls this in its **pre-run** checks. A token's stored `expires_at`
is a claim about time, not about the token, and the two come apart whenever one
dies early: revoked by its owner, deleted by an admin, or its records rebuilt
underneath it. Without this, a run completed against a token that had not
existed for hours and the upload failed at the very end, with the whole run
already spent.
