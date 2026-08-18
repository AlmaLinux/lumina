"""Record the certifications the catalog launches with.

The four systems published at almalinux.org/certification/ecosystem-catalog, with their
manufacturers, releases, and product pages as that catalog states them. A hardware certification
catalog that opens empty asks its first visitor to take on faith that it will be useful later;
these are real, already public, and the ones this project exists to list.

**A command and not a migration.** It was a migration first, and putting four published listings
and two vendors into every database broke 64 tests across twenty modules - every case that
counted the catalog, resolved a vendor that was not supposed to exist yet, or asserted on an
empty browse page. That is the signal: releases are infrastructure and belong in a migration
because nothing works without them, while *content* is a decision somebody makes once. Run it on
a fresh production install.

**The tier is who ran the tests, not who makes the hardware.** Fsas Technologies ran their own,
so those two are vendor-validated. The two Supermicro systems say "Tests Run By: AlmaLinux OS
Foundation", which is the almalinux tier here. Recording those as vendor-validated is the
conflation ``results.services.effective_level`` exists to prevent, and its docstring carries the
measurements from when this system had it the other way round.

**This records, it does not review.** Every other route to a published certification runs
through ``Submission.approve`` or ``approve_run``, and both cap what they grant -
``MANUAL_CEILING`` holds a hand-approved submission at community, because a tier above that has
to come from evidence a reviewer could check. These rows are written directly and so past the
cap, deliberately: the evidence is a published certification whose logs are in
AlmaLinux/certifications, and the cap exists to stop a tier being *invented* in the review UI,
not to stop the Foundation recording what it has already published.

Idempotent, and it only ever adds. A listing removed from the catalog by hand comes back on the
next run unless it is also removed from the table below - the same contract
``hardware/0003_reference_data`` has.

Two omissions on purpose. The processors are named in each description rather than attached as
CPU components: the reference families are seeded unpublished and the detail page links a
listing's CPUs unconditionally, so attaching one would put a link to a 404 on a production page.
And there are no category tags, because production seeds no taxonomy - the architecture facet is
derived from runs.
"""
from __future__ import annotations

from typing import override

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import CommunityAttestation, ListingVersion, Submission, System
from lumina.hardware.services import recompute_listing_levels
from lumina.releases.models import AlmaLinuxRelease
from lumina.vendors.models import Vendor

# The account the Foundation's own records are attributed to. ``attested_by`` is required and
# PROTECTed, so a record of this kind has to be somebody - and it must not be a person, who would
# then own claims they never personally made and could never be deleted.
SIG_USERNAME = "almalinux-certification-sig"
SIG_NAME = "AlmaLinux Certification SIG"

VENDORS = {
    "Fsas Technologies": "https://www.fsastech.com",
    "Supermicro": "https://www.supermicro.com",
}

# Each entry as the ecosystem catalog states it: the tier from "Tests Run By", the majors from
# the per-release certification blocks, the description from the manufacturer's own summary.
SYSTEMS = [
    {
        "vendor": "Fsas Technologies",
        "name": "PRIMERGY RX2540 M8 Rack Server",
        "model_number": "RX2540 M8",
        "description": (
            "Dual-socket 2U rack server for AI, business processing, graphics rendering, and "
            "in-memory databases, built on Intel Xeon 6 processors. Up to 32 DIMM modules "
            "(8TB DDR5), 12x 3.5-inch or 24x 2.5-inch storage devices, and 2 GPU cards."
        ),
        "vendor_spec_url": (
            "https://eu.fsastech.com/eu/products-services/primergy-servers/primergy-rx2540-m8/"
        ),
        "level": ValidationLevel.VENDOR,
        "majors": [9],
    },
    {
        "vendor": "Fsas Technologies",
        "name": "PRIMERGY RX2530 M8 Rack Server",
        "model_number": "RX2530 M8",
        "description": (
            "Dual-socket 1U rack server for virtualization, scale-out deployments, databases, "
            "and HPC, built on Intel Xeon 6 processors. Up to 32 DIMM modules (8TB DDR5) and "
            "4x 3.5-inch or 10x 2.5-inch storage devices."
        ),
        "vendor_spec_url": (
            "https://eu.fsastech.com/eu/products-services/primergy-servers/primergy-rx2530-m8/"
        ),
        "level": ValidationLevel.VENDOR,
        "majors": [9],
    },
    {
        "vendor": "Supermicro",
        "name": "A+ Server AS-2124BT-HNTR",
        "model_number": "AS-2124BT-HNTR",
        "description": (
            "2U four-node BigTwin, each node dual-socket AMD EPYC 7003/7002 with 16 DIMMs, "
            "sharing 12 hot-swap 3.5-inch NVMe/SAS/SATA bays."
        ),
        "vendor_spec_url": (
            "https://www.supermicro.com/en/Aplus/system/2U/2124/AS-2124BT-HNTR.cfm"
        ),
        "level": ValidationLevel.ALMALINUX,
        "majors": [8, 9],
    },
    {
        "vendor": "Supermicro",
        "name": "CloudDC SuperServer SYS-621C-TN12R",
        "model_number": "SYS-621C-TN12R",
        "description": (
            "2U CloudDC server with dual LGA-4677 sockets for 4th and 5th generation Intel "
            "Xeon Scalable processors, 16 DIMMs up to 4TB DDR5, and 12 hot-swap hybrid "
            "NVMe/SATA/SAS bays."
        ),
        "vendor_spec_url": (
            "https://www.supermicro.com/en/products/system/clouddc/2u/sys-621c-tn12r"
        ),
        "level": ValidationLevel.ALMALINUX,
        "majors": [8, 9],
    },
]

SOURCE_NOTE = (
    "Recorded from almalinux.org/certification/ecosystem-catalog. "
    "Test logs: github.com/AlmaLinux/certifications"
)


class Command(BaseCommand):
    help = "Record the four published AlmaLinux hardware certifications in the catalog."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be recorded without writing anything.",
        )

    @override
    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        recorded, skipped = [], []
        for spec in SYSTEMS:
            existing = System.objects.filter(
                vendor__name=spec["vendor"], name=spec["name"]).first()
            if existing is not None:
                skipped.append(spec["name"])
                continue
            recorded.append(spec["name"])
            if not dry_run:
                with transaction.atomic():
                    self._record(spec)

        for name in skipped:
            self.stdout.write(f"  already listed: {name}")
        verb = "Would record" if dry_run else "Recorded"
        if recorded:
            self.stdout.write(f"{verb} {len(recorded)} certification(s):")
            for name in recorded:
                self.stdout.write(f"  {name}")
        else:
            self.stdout.write("Nothing to record: all four are already listed.")

    def _record(self, spec: dict) -> None:
        sig = self._sig_account()
        vendor = self._vendor(spec["vendor"])
        releases = list(AlmaLinuxRelease.objects.filter(major__in=spec["majors"]))
        if len(releases) != len(spec["majors"]):
            # A certification is a statement about a release. With one missing there is nothing
            # to state, and inventing the release would be worse than listing nothing.
            raise SystemExit(
                f"{spec['name']} cites AlmaLinux {spec['majors']} and this database does not "
                "have all of them. Run migrate first."
            )

        system = System.objects.create(
            vendor=vendor, name=spec["name"], model_number=spec["model_number"],
            description=spec["description"], vendor_spec_url=spec["vendor_spec_url"],
            owner_vendor=vendor, published=True,
        )
        # One submission per machine, so the attestations have the source their CHECK constraint
        # requires and the audit trail says where this came from.
        submission = Submission.objects.create(
            submitter=sig, on_behalf_of=vendor, listing_system=system,
            claimed_validation_level=spec["level"], status=Submission.STATUS_APPROVED,
            reviewed_by=sig, reviewed_at=timezone.now(), submitter_notes=SOURCE_NOTE,
        )
        submission.cited_releases.set(releases)

        for release in releases:
            version = ListingVersion.objects.create(
                listing_system=system, release=release,
                # "Proven by a validation run" rather than "declared, not yet validated". The
                # run was real and its logs are published; it is not an alma-certify bundle, so
                # there is no TestRun to point at - which is why the attestation hangs off the
                # submission. Of the two values this field has, this is the true one.
                source=ListingVersion.SOURCE_RUN,
            )
            CommunityAttestation.objects.create(
                version=version, listing_system=system, submission=submission,
                attested_by=sig, level=spec["level"],
            )
        # Derived, not asserted: the tiers and the count come from the attestations just
        # written, so this cannot claim something the evidence underneath it does not support.
        recompute_listing_levels(system)

    @staticmethod
    def _sig_account():
        """The Foundation's record-keeping account.

        Inactive, with no usable password: it authenticates nowhere, which is the point. It
        exists so ``attested_by`` has something true to point at, and PROTECT then keeps it.
        """
        user_model = get_user_model()
        sig, created = user_model.objects.get_or_create(
            username=SIG_USERNAME,
            defaults={"first_name": SIG_NAME, "is_active": False},
        )
        if created:
            sig.set_unusable_password()
            sig.save(update_fields=["password"])
        return sig

    @staticmethod
    def _vendor(name: str) -> Vendor:
        """The manufacturer, verified and unowned.

        Verified records that these are named partners in a published certification program.
        Unowned so the claim flow stays open to somebody from the company: ``is_claimable``
        covers exactly this case, a vendor the SIG can vouch for before anyone from it has an
        account.
        """
        vendor, _ = Vendor.objects.get_or_create(
            name=name,
            defaults={"homepage": VENDORS[name], "scope": Vendor.SCOPE_HARDWARE,
                      "verified": True, "published": True},
        )
        return vendor
