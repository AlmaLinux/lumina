"""Idempotent seeder for the disposable devstack.

Creates:
  - Superuser ``admin`` / ``admin`` (override via env).
  - A ``reviewer`` Django group + user ``reviewer`` / ``reviewer`` in it,
    so the in-app review dashboard is reachable without poking around.
  - A handful of taxonomy Categories with sample approved values.
  - A couple of sample Vendors (one verified).

Safe to run repeatedly; it uses ``get_or_create`` throughout.
"""
from __future__ import annotations

import os
from io import BytesIO
from typing import NotRequired, TypedDict, override

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    CommunityAttestation,
    Component,
    ComponentKind,
    ListingCategoryValue,
    ListingVersion,
    Submission,
    System,
)
from lumina.hardware.services import recompute_listing_levels
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareAttestation,
    SoftwareCategoryValue,
    SoftwareCertification,
    SoftwareCompatibility,
)
from lumina.taxonomy.models import Category, CategoryValue, PickerWidget
from lumina.vendors.models import Vendor, VendorMembership

User = get_user_model()


class _SampleListing(TypedDict):
    """Schema for the inline sample-listing literals in _seed_sample_listings.

    A TypedDict (rather than ``list[dict]``) keeps the literal terse while
    giving pyright/Pyrefly enough info to type the per-key access - without
    that, every ``s["model"]`` was reported as Unknown.
    """

    model: type[System] | type[Component]
    name: str
    vendor: Vendor
    owner_vendor: Vendor | None
    model_number: str
    description: str
    level: str
    tags: list[tuple[str, str]]
    # (major, tier). ``tier`` empty means the release is only declared - the vendor states
    # support and no run has proven it - which the detail page renders differently from a
    # validated one. Certification is per major, so there is no minor here.
    releases: list[tuple[int, str]]
    cpus: NotRequired[list[str]]  # System-only

# The software categories the SIG asked for. Curated rather than suggestible:
# a browse facet whose values anyone can add fragments into near-duplicates
# ("AI", "A.I.", "Artificial Intelligence") faster than reviewers can merge them.
# Software has a single taxonomy category and these are its values, not ten
# categories of one value each. The slug is fixed here rather than derived from
# the name so it can never collide with a hardware category's slug, and so the
# query parameter (`?category=backup`) is stable if the label is ever reworded.
_SOFTWARE_CATEGORY_NAME = "Category"
_SOFTWARE_CATEGORY_SLUG = "category"
_SAMPLE_SOFTWARE_CATEGORIES: list[str] = [
    "AI", "Analytics", "Backup", "Cloud & Virtualization", "Creative",
    "Database", "Monitoring", "Networking", "Security", "Storage",
]

# --- real ecosystem software --------------------------------------------------
#
# Products that publicly state they run on AlmaLinux, so the catalog has realistic
# breadth to browse, filter, and page through instead of three invented rows.
#
# Every one of these is seeded at the **community** tier, and that is a
# correctness decision rather than laziness. "Vendor-validated" in this system
# means the vendor took part in the SIG's certification program; AlmaLinux's own
# site says software certification "is still in the works", so none of them have.
# Marking a real company as vendor-certified - or as a *verified* vendor, which is
# the SIG vouching for their identity - would put a claim in their mouth that they
# have not made. The invented products below cover the vendor and AlmaLinux tiers.
#
# ``majors`` is what each vendor documents, not what probably works. Where a
# vendor names AlmaLinux and its versions directly, that is what is recorded;
# where they ship el8/el9/el10 packages and name RHEL derivatives generally, the
# corresponding majors are recorded. Notable specifics:
#   - Veeam Agent supports 8 and 9 and explicitly does NOT support AlmaLinux 10.
#   - NAKIVO documents a floor of 8.7 within major 8.
#   - DaVinci Resolve is officially tested on Rocky 8.6 only; AlmaLinux support is
#     community knowledge, which is exactly what the community tier is for.
#
# (name, homepage)
_ECOSYSTEM_VENDORS: list[tuple[str, str]] = [
    ("Zabbix SIA", "https://www.zabbix.com/"),
    ("Checkmk GmbH", "https://checkmk.com/"),
    ("Grafana Labs", "https://grafana.com/"),
    ("Netdata Inc.", "https://www.netdata.cloud/"),
    ("Graylog, Inc.", "https://graylog.org/"),
    ("CloudLinux", "https://www.cloudlinux.com/"),
    ("Wazuh, Inc.", "https://wazuh.com/"),
    ("Open Information Security Foundation", "https://suricata.io/"),
    ("NAKIVO", "https://www.nakivo.com/"),
    ("Veeam Software", "https://www.veeam.com/"),
    ("Bareos GmbH & Co. KG", "https://www.bareos.com/"),
    ("Percona", "https://www.percona.com/"),
    ("MariaDB Foundation", "https://mariadb.org/"),
    ("PostgreSQL Global Development Group", "https://www.postgresql.org/"),
    ("MongoDB, Inc.", "https://www.mongodb.com/"),
    ("Docker, Inc.", "https://www.docker.com/"),
    ("Nextcloud GmbH", "https://nextcloud.com/"),
    ("Kasm Technologies", "https://www.kasmweb.com/"),
    ("MinIO, Inc.", "https://min.io/"),
    ("Ceph Foundation", "https://ceph.io/"),
    ("Sangoma Technologies", "https://www.asterisk.org/"),
    ("OpenVPN Inc.", "https://openvpn.net/"),
    ("Metabase", "https://www.metabase.com/"),
    ("Apache Software Foundation", "https://superset.apache.org/"),
    ("NVIDIA", "https://www.nvidia.com/"),
    ("Ollama", "https://ollama.com/"),
    ("Blackmagic Design", "https://www.blackmagicdesign.com/"),
    ("Blender Foundation", "https://www.blender.org/"),
]


class _EcosystemSoftware(TypedDict):
    """One real product's stated AlmaLinux support."""

    vendor: str
    name: str
    description: str
    categories: list[str]
    majors: list[int]


_ECOSYSTEM_SOFTWARE: list[_EcosystemSoftware] = [
    # --- Monitoring ---
    {"vendor": "Zabbix SIA", "name": "Zabbix",
     "description": "Enterprise monitoring for networks, servers, and applications. "
                    "Ships dedicated AlmaLinux 8, 9, and 10 package repositories.",
     "categories": ["Monitoring", "Networking"], "majors": [8, 9, 10]},
    {"vendor": "Checkmk GmbH", "name": "Checkmk",
     "description": "Infrastructure and application monitoring. Names AlmaLinux "
                    "among its supported RHEL-compatible distributions.",
     "categories": ["Monitoring"], "majors": [8, 9]},
    {"vendor": "Grafana Labs", "name": "Grafana",
     "description": "Dashboards and visualization for metrics, logs, and traces.",
     "categories": ["Monitoring", "Analytics"], "majors": [8, 9]},
    {"vendor": "Netdata Inc.", "name": "Netdata",
     "description": "Per-second infrastructure metrics with automatic dashboards.",
     "categories": ["Monitoring"], "majors": [8, 9]},
    {"vendor": "Graylog, Inc.", "name": "Graylog",
     "description": "Centralized log management and analysis. Documents "
                    "AlmaLinux 8, 9, and 10.",
     "categories": ["Monitoring", "Security", "Analytics"], "majors": [8, 9, 10]},

    # --- Security ---
    {"vendor": "CloudLinux", "name": "Imunify360",
     "description": "Server security suite with a web application firewall and "
                    "malware scanning. Officially supports AlmaLinux 8, 9, and 10.",
     "categories": ["Security"], "majors": [8, 9, 10]},
    {"vendor": "Wazuh, Inc.", "name": "Wazuh",
     "description": "Open source XDR and SIEM platform. AlmaLinux 9 and later for "
                    "the manager, indexer, and dashboard components.",
     "categories": ["Security", "Monitoring"], "majors": [9]},
    {"vendor": "Open Information Security Foundation", "name": "Suricata",
     "description": "High performance network IDS, IPS, and network security "
                    "monitoring engine.",
     "categories": ["Security", "Networking"], "majors": [8, 9]},

    # --- Backup ---
    {"vendor": "NAKIVO", "name": "NAKIVO Backup & Replication",
     "description": "Backup, replication, and recovery for virtual, physical, and "
                    "cloud workloads. Supports AlmaLinux 8.7 and later.",
     "categories": ["Backup", "Cloud & Virtualization"], "majors": [8, 9, 10]},
    {"vendor": "Veeam Software", "name": "Veeam Agent for Linux",
     "description": "Agent-based backup and recovery for Linux servers and "
                    "workstations. AlmaLinux 8 and 9; AlmaLinux 10 is not yet "
                    "supported by the vendor.",
     "categories": ["Backup"], "majors": [8, 9]},
    {"vendor": "Bareos GmbH & Co. KG", "name": "Bareos",
     "description": "Network backup, archiving, and recovery, forked from Bacula.",
     "categories": ["Backup", "Storage"], "majors": [8, 9]},

    # --- Database ---
    {"vendor": "Percona", "name": "Percona Server for MySQL",
     "description": "Drop-in MySQL replacement with enterprise features. "
                    "Packages published for AlmaLinux 8 and 9.",
     "categories": ["Database"], "majors": [8, 9]},
    {"vendor": "MariaDB Foundation", "name": "MariaDB Server",
     "description": "Relational database server, shipped in AlmaLinux's own "
                    "AppStream repositories.",
     "categories": ["Database"], "majors": [8, 9, 10]},
    {"vendor": "PostgreSQL Global Development Group", "name": "PostgreSQL",
     "description": "Object-relational database system. The PGDG yum repository "
                    "publishes builds for AlmaLinux 8, 9, and 10.",
     "categories": ["Database"], "majors": [8, 9, 10]},
    {"vendor": "MongoDB, Inc.", "name": "MongoDB Community Server",
     "description": "Document-oriented database with RHEL-compatible packages.",
     "categories": ["Database"], "majors": [8, 9]},

    # --- Cloud & Virtualization ---
    {"vendor": "Docker, Inc.", "name": "Docker Engine",
     "description": "Container runtime and tooling, with el8, el9, and el10 "
                    "packages for RHEL-compatible distributions.",
     "categories": ["Cloud & Virtualization"], "majors": [8, 9, 10]},
    {"vendor": "Nextcloud GmbH", "name": "Nextcloud Server",
     "description": "Self-hosted file sync, share, and collaboration platform.",
     "categories": ["Cloud & Virtualization", "Storage"], "majors": [8, 9, 10]},
    {"vendor": "Kasm Technologies", "name": "Kasm Workspaces",
     "description": "Container streaming for browser-delivered desktops and apps. "
                    "Publishes AlmaLinux 8 and 9 desktop images.",
     "categories": ["Cloud & Virtualization", "Creative"], "majors": [8, 9]},

    # --- Storage ---
    {"vendor": "MinIO, Inc.", "name": "MinIO",
     "description": "S3-compatible high performance object storage.",
     "categories": ["Storage"], "majors": [8, 9, 10]},
    {"vendor": "Ceph Foundation", "name": "Ceph",
     "description": "Distributed object, block, and file storage at scale.",
     "categories": ["Storage"], "majors": [9, 10]},

    # --- Networking ---
    {"vendor": "Sangoma Technologies", "name": "Asterisk",
     "description": "Telephony and real-time communications framework.",
     "categories": ["Networking"], "majors": [8, 9]},
    {"vendor": "OpenVPN Inc.", "name": "OpenVPN Access Server",
     "description": "Self-hosted VPN server with a management interface.",
     "categories": ["Networking", "Security"], "majors": [8, 9]},

    # --- Analytics ---
    {"vendor": "Metabase", "name": "Metabase",
     "description": "Self-service business intelligence and dashboards, run on "
                    "the JVM.",
     "categories": ["Analytics"], "majors": [8, 9, 10]},
    {"vendor": "Apache Software Foundation", "name": "Apache Superset",
     "description": "Data exploration and visualization platform.",
     "categories": ["Analytics", "Database"], "majors": [8, 9]},

    # --- AI ---
    {"vendor": "NVIDIA", "name": "NVIDIA CUDA Toolkit",
     "description": "GPU computing toolkit and drivers. AlmaLinux 8, 9, and 10 are "
                    "officially supported, and AlmaLinux hosts an NVIDIA driver "
                    "repository directly.",
     "categories": ["AI"], "majors": [8, 9, 10]},
    {"vendor": "Ollama", "name": "Ollama",
     "description": "Local runner for open large language models.",
     "categories": ["AI"], "majors": [8, 9, 10]},

    # --- Creative ---
    {"vendor": "Blackmagic Design", "name": "DaVinci Resolve",
     "description": "Colour grading, editing, and post-production suite. The vendor "
                    "tests on Rocky Linux only, so AlmaLinux support here is "
                    "community knowledge rather than a vendor statement.",
     "categories": ["Creative"], "majors": [9]},
    {"vendor": "Blender Foundation", "name": "Blender",
     "description": "3D creation suite for modelling, animation, and rendering.",
     "categories": ["Creative", "AI"], "majors": [8, 9, 10]},
]

# (name, verified, homepage)
_SAMPLE_SOFTWARE_VENDORS: list[tuple[str, bool, str]] = [
    ("Vaultwise", True, "https://example.com/vaultwise"),
    ("Meshsight", True, "https://example.com/meshsight"),
    ("Orbital Forge", False, "https://example.com/orbitalforge"),
]

# The two shapes this catalog exists to represent, seeded so both are visible
# without any manual setup:
#
#  - "Vaultwise Archive" is vendor-certified on 8 and 9 but only community-backed
#    on 10. That is vendor abandonment made legible: the listing badge still reads
#    Vendor-validated (highest across majors), and the per-major breakdown is what
#    shows the decay.
#  - "Orbital Forge Studio" is an unclaimed community listing whose vendor was
#    created inline by a community member, so the claim flow is exercisable.
#
# (vendor, name, categories, {major: tier or None for community-only})
_SAMPLE_SOFTWARE: list[tuple[str, str, list[str], dict[int, str | None]]] = [
    ("Vaultwise", "Vaultwise Archive", ["Backup", "Storage"],
     {8: "vendor", 9: "vendor", 10: None}),
    ("Meshsight", "Meshsight Collector",
     ["Monitoring", "Networking"], {9: "almalinux", 10: "vendor"}),
    ("Orbital Forge", "Orbital Forge Studio", ["Creative"], {9: None}),
]

# Architecture is the only hardware facet left, and the only one that earns its
# place: every validation run reports the kernel's arch, so it is filled in for
# every listing that has evidence, without anybody being asked.
#
# Network, Storage, and PCIe Generation used to be here and were removed. A facet
# is only useful if it is populated consistently, and those were not - link speeds
# and disk transports only appear when a run happened to see them, and PCIe
# generation is not visible to the collector at all. A filter that is set on some
# listings and blank on others reads as "no such hardware" when it means "nobody
# recorded it".
#
# ``allow_suggestions=False`` and no proposal path: the arch list is what the
# Foundation builds for, not something a submitter extends.
_SAMPLE_TAXONOMY: list[tuple[str, list[str], PickerWidget, bool]] = [
    ("Architecture",     ["x86_64", "aarch64", "s390x", "ppc64le"], PickerWidget.dropdown,   False),
    # "Certified for" previously lived here; it's now a dedicated model
    # (lumina.releases.AlmaLinuxRelease) seeded separately.
]

# (major, supported, latest_minor).
#
# ``latest_minor`` is the newest minor that has actually shipped, and it is what lifts the
# "enablement lands in 10.3" disclaimer off a listing proved on AlmaLinux Kitten. Seeded with
# real-looking values rather than left on the field default: the default is 0 for a new row, and
# the old ``max_minor`` this replaced defaulted to 10 - which on a carried-over row would claim
# every major has shipped ten minors and quietly lift every gate.
_SAMPLE_ALMA_RELEASES: list[tuple[int, bool, int]] = [
    (8,  True, 10),
    (9,  True, 6),
    # Kitten tracks 10 at the time of writing, so a run on it anticipates an upcoming 10.x.
    # Nothing records that here: a Kitten run says which major it is on itself.
    (10, True, 0),
]

# (name, verified, homepage, logo_url). Empty logo_url falls back to a
# locally-generated placeholder badge. Remote URLs are fetched once when
# the vendor is first seeded (idempotent: skipped if a logo is already set).
_SAMPLE_VENDORS: list[tuple[str, bool, str, str]] = [
    (
        "Dell", True, "https://www.dell.com",
        "https://access.redhat.com/hydra/cwe/rest/v1.0/public/partners/564057/logo",
    ),
    (
        "Supermicro", True, "https://www.supermicro.com",
        "https://access.redhat.com/hydra/cwe/rest/v1.0/public/partners/197/logo",
    ),
    (
        "Intel", True, "https://www.intel.com",
        "https://access.redhat.com/hydra/cwe/rest/v1.0/public/partners/6/logo",
    ),
    ("AMD",                 True,  "https://www.amd.com",   ""),
    ("Community-Submitted", False, "",                      ""),
]

_SAMPLE_CPUS: list[dict] = [
    {"name": "Intel® Xeon® 6 Processors", "vendor": "Intel", "model_number": "", "description": "Intel Corporation Intel® Xeon® Processors Intel® Xeon® 6"},
    {"name": "AMD EPYC™ 9004 Series",             "vendor": "AMD",   "model_number": "",   "description": "AMD EPYC™ 9004 Series"},
]


class Command(BaseCommand):
    help = "Seed the devstack with an admin user, a reviewer, sample taxonomy, and vendors."

    @override
    def handle(self, *args, **options):
        self._seed_users()
        self._seed_taxonomy()
        self._seed_alma_releases()
        self._seed_vendors()
        self._seed_pci_vendor_aliases()
        self._seed_sample_memberships()
        self._seed_sample_cpus()
        self._seed_sample_listings()
        self._seed_software_categories()
        self._seed_software_vendors()
        self._seed_sample_software()
        self._seed_ecosystem_vendors()
        self._seed_ecosystem_software()
        self.stdout.write(self.style.SUCCESS("Devstack seeded."))

    def _seed_users(self) -> None:
        admin_pw = os.environ.get("DEVSTACK_ADMIN_PASSWORD", "admin")
        reviewer_pw = os.environ.get("DEVSTACK_REVIEWER_PASSWORD", "reviewer")

        admin, created = User.objects.get_or_create(
            username="admin",
            defaults={"email": "admin@example.com", "is_staff": True, "is_superuser": True},
        )
        if created:
            admin.set_password(admin_pw)
            admin.save()
            self.stdout.write(f"  created superuser admin/{admin_pw}")
        # Membership in the `admin` Django group is what grants reviewer
        # access via lumina.review.permissions.is_reviewer. Django's
        # is_superuser flag alone is intentionally insufficient - we want
        # review eligibility to be a group decision even for superusers.
        admin_group, _ = Group.objects.get_or_create(name="admin")
        admin.groups.add(admin_group)

        # The Certification SIG: may certify on AlmaLinux's behalf without the
        # superuser escalation that comes with the admin group. Created empty
        # so the group exists to assign people to.
        Group.objects.get_or_create(name="certifier")

        reviewer_group, _ = Group.objects.get_or_create(name="reviewer")
        reviewer, created = User.objects.get_or_create(
            username="reviewer",
            defaults={"email": "reviewer@example.com"},
        )
        if created:
            reviewer.set_password(reviewer_pw)
            reviewer.save()
            self.stdout.write(f"  created reviewer reviewer/{reviewer_pw}")
        reviewer.groups.add(reviewer_group)

    def _seed_taxonomy(self) -> None:
        for cat_name, values, picker_widget, allow_suggestions in _SAMPLE_TAXONOMY:
            category, created = Category.objects.get_or_create(
                name=cat_name,
                defaults={
                    "slug": cat_name.lower().replace(" ", "-"),
                    "picker_widget": picker_widget.value,
                    "allow_suggestions": allow_suggestions,
                    # Architecture is the only hardware facet, and it is filled in
                    # from run evidence rather than asked for.
                    "derived_from_runs": True,
                },
            )
            # Backfill the flags on existing seeded categories so re-runs pick
            # up changes without requiring a fresh DB.
            update_fields: list[str] = []
            if not category.derived_from_runs:
                category.derived_from_runs = True
                update_fields.append("derived_from_runs")
            if category.picker_widget != picker_widget.value:
                category.picker_widget = picker_widget.value
                update_fields.append("picker_widget")
            if category.allow_suggestions != allow_suggestions:
                category.allow_suggestions = allow_suggestions
                update_fields.append("allow_suggestions")
            if update_fields:
                category.save(update_fields=update_fields)
            for v in values:
                CategoryValue.objects.get_or_create(category=category, value=v)

    def _seed_software_categories(self) -> None:
        """One category, "Category", whose values are the software types.

        A category is a *question* and its values are the answers, so the
        question is "what kind of software is this?" and Backup, AI, and the rest
        are answers to it. Seeding each name as its own single-value Category
        instead renders as one sidebar card per name whose header and only
        checkbox say the same word twice.

        Keyed on a slug this app owns rather than on ``slugify(name)``: software
        wants a Storage facet and hardware already has a Storage *category*, so a
        name-derived key reaches a row belonging to the other catalog and flips
        its scope out from under it.

        ``applies_to=software`` is what keeps this out of the hardware filter
        panel and vice versa; the pending/approve workflow, picker widgets, and
        collapse behaviour all come along for free.
        """
        category, _ = Category.objects.get_or_create(
            slug=_SOFTWARE_CATEGORY_SLUG,
            defaults={
                "name": _SOFTWARE_CATEGORY_NAME,
                "applies_to": Category.APPLIES_SOFTWARE,
                "picker_widget": PickerWidget.checkboxes.value,
                "allow_suggestions": False,
                # Show all ten. Past this the panel collapses the overflow and
                # adds a search box, which is the right behaviour once the SIG
                # has enough categories to need it and only clutter before then.
                "collapsed_limit": 10,
                "display_order": 200,
            },
        )
        # Backfill on re-run, same as _seed_taxonomy: an existing row from an
        # earlier seed should pick up the scope rather than stay hardware-ish.
        update_fields: list[str] = []
        if category.applies_to != Category.APPLIES_SOFTWARE:
            category.applies_to = Category.APPLIES_SOFTWARE
            update_fields.append("applies_to")
        if category.name != _SOFTWARE_CATEGORY_NAME:
            category.name = _SOFTWARE_CATEGORY_NAME
            update_fields.append("name")
        if update_fields:
            category.save(update_fields=update_fields)

        for value in _SAMPLE_SOFTWARE_CATEGORIES:
            CategoryValue.objects.get_or_create(category=category, value=value)

    def _seed_software_vendors(self) -> None:
        for name, verified, homepage in _SAMPLE_SOFTWARE_VENDORS:
            vendor, _ = Vendor.objects.get_or_create(
                slug=slugify(name),
                defaults={
                    "name": name, "verified": verified, "homepage": homepage,
                    "scope": Vendor.SCOPE_SOFTWARE,
                },
            )
            update_fields = []
            if vendor.scope != Vendor.SCOPE_SOFTWARE:
                vendor.scope = Vendor.SCOPE_SOFTWARE
                update_fields.append("scope")
            if vendor.verified != verified:
                vendor.verified = verified
                update_fields.append("verified")
            if update_fields:
                vendor.save(update_fields=update_fields)

    def _seed_ecosystem_vendors(self) -> None:
        """Publishers of the real products, as software-scoped vendors.

        ``verified=False`` deliberately: verification is the SIG vouching that a
        person represents this company, and nobody has claimed any of these. It
        also keeps them claimable, so the claim flow has plenty of realistic
        targets, and keeps ``derive_allowed_levels`` from handing out the vendor
        tier on an identity nobody has proven.
        """
        for name, homepage in _ECOSYSTEM_VENDORS:
            vendor, created = Vendor.objects.get_or_create(
                slug=slugify(name),
                defaults={
                    "name": name, "homepage": homepage, "published": True,
                    "verified": False, "scope": Vendor.SCOPE_SOFTWARE,
                },
            )
            # A vendor of this name may already exist from the hardware samples
            # (NVIDIA is both). Widen its scope rather than fork the identity.
            if not created and vendor.scope == Vendor.SCOPE_HARDWARE:
                vendor.scope = Vendor.SCOPE_BOTH
                vendor.save(update_fields=["scope"])

    def _seed_ecosystem_software(self) -> None:
        """Real products that publicly state they run on AlmaLinux.

        All community-tier, all left unowned. See the comment on
        ``_ECOSYSTEM_SOFTWARE`` for why that is a correctness decision and not a
        shortcut.
        """
        for spec in _ECOSYSTEM_SOFTWARE:
            vendor = Vendor.objects.filter(slug=slugify(spec["vendor"])).first()
            if vendor is None:
                continue
            product, created = Software.objects.get_or_create(
                slug=slugify(f"{vendor.slug}-{spec['name']}"),
                defaults={
                    "vendor": vendor,
                    "name": spec["name"],
                    "description": spec["description"],
                    "published": True,
                    # Unowned: nobody from these companies has claimed a listing,
                    # so edit rights stay with admins and the "are you the vendor?"
                    # path stays open.
                    "owner_vendor": None,
                },
            )
            if not created and product.description != spec["description"]:
                product.description = spec["description"]
                product.save(update_fields=["description"])

            for cat_name in spec["categories"]:
                value = CategoryValue.objects.filter(
                    category__slug=_SOFTWARE_CATEGORY_SLUG, value=cat_name
                ).first()
                if value is not None:
                    SoftwareCategoryValue.objects.get_or_create(
                        software=product, value=value
                    )

            for major in spec["majors"]:
                release = AlmaLinuxRelease.objects.filter(major=major).first()
                if release is None:
                    continue
                row, _ = SoftwareCompatibility.objects.get_or_create(
                    software=product, release=release
                )
                # Varied counts rather than a flat number, so ordering and the
                # "x N attestations" line look like real data. Deterministic:
                # a seeder that shuffled would churn the database on every run.
                self._seed_software_confirmations(
                    row, count=1 + (major + len(spec["name"])) % 4,
                )
            product.recompute_levels()

    def _seed_sample_software(self) -> None:
        reviewer = User.objects.filter(username="reviewer").first()
        for vendor_name, name, categories, majors in _SAMPLE_SOFTWARE:
            vendor = Vendor.objects.filter(slug=slugify(vendor_name)).first()
            if vendor is None:
                continue
            product, _ = Software.objects.get_or_create(
                slug=slugify(f"{vendor.slug}-{name}"),
                defaults={
                    "vendor": vendor, "name": name, "published": True,
                    "description": f"Sample {name} listing for devstack.",
                    # Orbital Forge is deliberately left ownerless so the claim
                    # flow has something to claim.
                    "owner_vendor": None if vendor_name == "Orbital Forge" else vendor,
                },
            )
            for cat_name in categories:
                value = CategoryValue.objects.filter(
                    category__slug=_SOFTWARE_CATEGORY_SLUG, value=cat_name
                ).first()
                if value is not None:
                    SoftwareCategoryValue.objects.get_or_create(
                        software=product, value=value
                    )
            for major, tier in majors.items():
                release = AlmaLinuxRelease.objects.filter(major=major).first()
                if release is None:
                    continue
                row, _ = SoftwareCompatibility.objects.get_or_create(
                    software=product, release=release
                )
                if tier is not None:
                    SoftwareCertification.objects.get_or_create(
                        compatibility=row, level=tier,
                        defaults={"certified_by": reviewer},
                    )
                # Community confirmations on every major, including the certified
                # ones. That pairing is the point: a vendor-validated release still
                # shows the community count beside it rather than replacing it, and
                # a community-only release needs at least one or its tier is a claim
                # nobody made. More than one so the browse card's
                # "x N attestations" line has something to show.
                self._seed_software_confirmations(
                    row, count=2 if tier is not None else 3,
                )
            product.recompute_levels()

    def _seed_software_confirmations(self, row, *, count: int) -> None:
        """Add ``count`` distinct community confirmations to a compatibility row.

        One per user per major is enforced by the model, so each needs its own
        person; re-running the seeder reuses them rather than piling up.
        """
        for index in range(count):
            username = f"seed-confirmer-{index + 1}"
            user, _ = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@example.com"},
            )
            SoftwareAttestation.objects.get_or_create(compatibility=row, user=user)

    def _seed_vendors(self) -> None:
        for name, verified, homepage, logo_url in _SAMPLE_VENDORS:
            # Key lookup on slug, not name: users (or earlier test flows)
            # may rename a seeded vendor via the proposal system, and the
            # seeder must not then try to recreate it on its old name.
            slug = slugify(name)
            vendor, _ = Vendor.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "verified": verified, "homepage": homepage},
            )
            if vendor.logo:
                continue
            # Prefer the configured remote logo; fall back to a locally drawn
            # badge if the fetch fails (offline dev, URL moved, etc.).
            logo = None
            if logo_url:
                logo = self._fetch_remote_logo(logo_url)
                if logo is None:
                    self.stdout.write(self.style.WARNING(
                        f"  could not fetch logo for {name!r}, using placeholder"
                    ))
            if logo is None:
                logo = self._generate_placeholder_logo(name)
            if logo is not None:
                vendor.logo.save(f"{vendor.slug}.png", logo, save=True)

    @staticmethod
    def _fetch_remote_logo(url: str) -> ContentFile | None:
        """Download a logo from a public URL. Returns None on any failure so
        the caller can fall back to a placeholder - we don't want offline
        ``seed_devstack`` runs to blow up."""
        import urllib.request
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                return ContentFile(resp.read())
        except Exception:
            return None

    @staticmethod
    def _generate_placeholder_logo(name: str) -> ContentFile | None:
        """Render a simple colored badge with the vendor's initial letter so
        sample vendors render a logo without needing real brand assets."""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return None

        # Stable color per name so re-runs produce the same logo.
        seed = sum(ord(c) for c in name)
        palette = [
            (8, 35, 54),    # AlmaLinux navy
            (0, 75, 188),   # AlmaLinux blue
            (131, 24, 131), # AlmaLinux purple
            (40, 90, 130),
            (60, 70, 100),
        ]
        bg = palette[seed % len(palette)]

        img = Image.new("RGB", (240, 80), bg)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 36)
        except OSError:
            font = ImageFont.load_default()
        text = name[:18]
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((240 - tw) / 2, (80 - th) / 2 - bbox[1]), text, fill="white", font=font)

        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return ContentFile(buf.getvalue())

    def _seed_alma_releases(self) -> None:
        for major, supported, latest_minor in _SAMPLE_ALMA_RELEASES:
            release, created = AlmaLinuxRelease.objects.get_or_create(
                major=major,
                defaults={"supported": supported, "latest_minor": latest_minor},
            )
            # Backfilled on re-run, as the rest of the seeder does. A stack seeded before these
            # fields existed carries the old ``max_minor`` default of 10, which reads as "ten
            # minors have shipped" and lifts every timing gate on the stack.
            if not created and release.latest_minor != latest_minor:
                release.latest_minor = latest_minor
                release.save(update_fields=["latest_minor"])

    def _seed_pci_vendor_aliases(self) -> None:
        """The pci.ids spellings, now that the vendors they point at exist.

        The ``vendors.0002`` data migration applies the same table, but it can only alias a vendor
        that already exists - and on a fresh database it runs long before this seeder creates any.
        Without this, a seeded stack has an AMD vendor and no way to resolve
        "Advanced Micro Devices, Inc. [AMD/ATI]" to it, so an AMD GPU is orphaned from the
        families AMD owns.
        """
        from lumina.vendors.models import Vendor, VendorAlias
        from lumina.vendors.pci_aliases import ensure

        added = ensure(Vendor, VendorAlias)
        if added:
            self.stdout.write(f"  pci.ids vendor aliases: {added} added")

    def _seed_sample_memberships(self) -> None:
        """Give the seeded users vendor memberships so they can exercise the
        full submission-on-behalf and edit-proposal flows without manual setup."""
        memberships = [
            ("admin",    "Dell",  VendorMembership.ROLE_OWNER),
            ("reviewer", "Intel", VendorMembership.ROLE_SUBMITTER),
        ]
        for username, vendor_name, role in memberships:
            user = User.objects.filter(username=username).first()
            vendor = Vendor.objects.filter(name=vendor_name).first()
            if user and vendor:
                VendorMembership.objects.get_or_create(
                    user=user, vendor=vendor, defaults={"role": role}
                )

    def _seed_sample_cpus(self) -> None:
        for spec in _SAMPLE_CPUS:
            vendor = Vendor.objects.filter(name=spec["vendor"]).first()
            if not vendor:
                continue
            Component.objects.get_or_create(
                name=spec["name"],
                vendor=vendor,
                defaults={
                    "model_number": spec["model_number"],
                    "description": spec["description"],
                    "kind": ComponentKind.cpu.value,
                    "published": True,
                    # No tier and no count written here. Both are *derived* columns - the same
                    # reason ``_seed_attestations`` below exists rather than setting a tier
                    # directly - so "vendor-validated, 1 attestation" with no attestation row
                    # behind it was a claim the sample data could not support, and the first
                    # recompute would have taken it away. These rows are here to give the CPU
                    # pickers something to match against, which needs neither.
                },
            )

    # How many people have confirmed each tier in the sample data. Community rows
    # get more than one so the count column has something to show, and so
    # "one per person per release" is visibly a per-person rule.
    _ATTESTERS_PER_TIER = {ValidationLevel.COMMUNITY: 3}

    def _seed_attestations(self, version, listing, tier: str, fk: str) -> None:
        """Back a seeded release's tier with real attestation rows.

        The tier on a ``ListingVersion`` is derived, so writing it directly would
        be undone by the next recompute. Seeding the evidence instead means the
        sample data behaves like data the application produced.

        Each attestation needs exactly one source
        (``attestation_exactly_one_source``); a ``Submission`` is used because a
        seeded ``TestRun`` would need a whole bundle behind it.

        One way this sample data no longer resembles production: rows are written
        directly rather than through ``Submission.approve``, so the vendor and
        AlmaLinux tiers here are attributed to submissions, which that method now caps
        at ``Submission.MANUAL_CEILING``. A real vendor tier can only come from an
        approved run. Seeding it honestly means building bundles here, which is a
        bigger job than this helper; until then the tiers are right and their
        attribution is a fixture artifact.
        """
        for index in range(self._ATTESTERS_PER_TIER.get(tier, 1)):
            username = f"seed-{tier}-{index + 1}"
            attester, _ = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@example.com"},
            )
            existing = CommunityAttestation.objects.filter(
                version=version, attested_by=attester
            ).first()
            if existing is not None:
                continue
            submission = Submission.objects.create(
                submitter=attester,
                claimed_validation_level=tier,
                status=Submission.STATUS_APPROVED,
                **{fk: listing},
            )
            # Kept consistent with the attestation even though nothing reads it back
            # here: an approved submission with no citations is a state the form cannot
            # produce, and leaving it empty would make the seed data lie about what it
            # claimed.
            submission.cited_releases.set([version.release])
            CommunityAttestation.objects.create(
                version=version,
                attested_by=attester,
                level=tier,
                submission=submission,
                **{fk: listing},
            )

    def _seed_sample_listings(self) -> None:
        """A couple of published listings so the catalog isn't empty on first visit."""
        # Look up by slug - vendors may have been renamed via the proposal
        # flow but slugs stay stable.
        dell = Vendor.objects.filter(slug="dell").first()
        supermicro = Vendor.objects.filter(slug="supermicro").first()
        if not dell or not supermicro:
            return

        def _get_value(cat_name: str, value: str) -> CategoryValue | None:
            try:
                return CategoryValue.objects.get(category__name=cat_name, value=value)
            except CategoryValue.DoesNotExist:
                return None

        samples: list[_SampleListing] = [
            {
                "model": System,
                "name": "PowerEdge R750",
                "vendor": dell,
                # owner_vendor=Dell so Dell submit-role members can later
                # propose edits via the self-service flow.
                "owner_vendor": dell,
                "model_number": "R750",
                "description": "2U dual-socket rack server with up to 32 DIMMs and 24 NVMe bays.",
                "level": ValidationLevel.VENDOR,
                "tags": [("Architecture", "x86_64")],
                "cpus": ["Intel® Xeon® 6 Processors", "AMD EPYC™ 9004 Series"],
                # The abandonment shape, seeded so it is visible without setup:
                # Dell validated 9 and stopped, and the only evidence on 10 came
                # from the community. The listing badge still reads
                # Vendor-validated (highest across releases), and the per-release
                # table on the detail page is where that decay is legible.
                "releases": [(9, ValidationLevel.VENDOR),
                             (10, ValidationLevel.COMMUNITY)],
            },
            {
                "model": System,
                "name": "SuperServer SYS-221H-TN24R",
                "vendor": supermicro,
                "owner_vendor": supermicro,
                "model_number": "SYS-221H-TN24R",
                "description": "2U Hyper-Twin with dual Intel Xeon scalable CPUs.",
                "level": ValidationLevel.ALMALINUX,
                "tags": [("Architecture", "x86_64")],
                "cpus": ["Intel® Xeon® 6 Processors"],
                # A declared release beside a validated one, so the detail page's
                # proven-versus-declared distinction has something to show.
                "releases": [(10, ValidationLevel.ALMALINUX), (9, "")],
            },
            {
                "model": Component,
                "name": "BCM57414 25GbE NIC",
                "vendor": dell,
                # Community-submitted: owner_vendor stays None so no vendor
                # member can edit it via the self-service flow - only admins.
                "owner_vendor": None,
                "model_number": "BCM57414",
                "description": "Dual-port 25GbE NIC certified on AlmaLinux 9 and 10.",
                "level": ValidationLevel.COMMUNITY,
                "tags": [("Architecture", "x86_64")],
                "releases": [(9, ValidationLevel.COMMUNITY),
                             (10, ValidationLevel.COMMUNITY)],
            },
        ]

        for s in samples:
            obj, created = s["model"].objects.get_or_create(
                name=s["name"],
                vendor=s["vendor"],
                defaults={
                    "model_number": s["model_number"],
                    "description": s["description"],
                    "owner_vendor": s.get("owner_vendor"),
                    "published": True,
                    "validation_level": s["level"],
                    "attestation_count": 1,
                },
            )
            # Backfill owner_vendor on existing seeded listings so re-runs
            # against an older devstack pick up the new ownership semantics.
            # Use queryset.update() rather than instance attribute assignment
            # to sidestep the FK descriptor's static-type complaints.
            desired_owner = s.get("owner_vendor")
            if not created and obj.owner_vendor != desired_owner:
                type(obj).objects.filter(pk=obj.pk).update(owner_vendor=desired_owner)

            # Tags / versions / cpu attachments run on every seed (idempotent
            # via get_or_create / M2M.add) so re-runs pick up changes to the
            # sample spec (e.g. swapping CPUs to family-style names).
            fk = "listing_system" if isinstance(obj, System) else "listing_component"
            for cat_name, value in s["tags"]:
                cv = _get_value(cat_name, value)
                if cv:
                    ListingCategoryValue.objects.get_or_create(value=cv, **{fk: obj})
            for major, tier in s.get("releases", []):
                release = AlmaLinuxRelease.objects.filter(major=major).first()
                if release is None:
                    continue
                # A tier means evidence proved it; no tier means the vendor only
                # declared it.
                source = (
                    ListingVersion.SOURCE_RUN if tier
                    else ListingVersion.SOURCE_DECLARED
                )
                version, created = ListingVersion.objects.get_or_create(
                    release=release, **{fk: obj}, defaults={"source": source},
                )
                # Backfill on re-run, as the rest of the seeder does. Without it a
                # row seeded before this field mattered keeps source=declared
                # while carrying a tier, and the detail page contradicts itself.
                if not created and version.source != source:
                    version.source = source
                    version.save(update_fields=["source"])
                if tier:
                    self._seed_attestations(version, obj, tier, fk)
            # After the releases, so the derived columns match the evidence just
            # written rather than the literal in the sample spec.
            recompute_listing_levels(obj)
            if isinstance(obj, System):
                for cpu_name in s.get("cpus", []):
                    cpu = Component.objects.filter(name=cpu_name, kind=ComponentKind.cpu.value).first()
                    if cpu:
                        obj.cpus.add(cpu)
