"""DRF serializers.

Kept intentionally minimal for v1: they mirror the public-facing HTML and
are the contract for external consumers. Keep any internal-only fields
(reviewer notes, audit snapshots, submitter identity details) OUT of these.
"""
from __future__ import annotations

from django.db.models import Count
from rest_framework import serializers

from lumina.hardware.models import Component, System
from lumina.results.models import BenchmarkResult, TestResult, TestRun
from lumina.software.models import Software
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor


class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = ["slug", "name", "homepage", "verified"]


class CategoryValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryValue
        fields = ["slug", "value"]


class CategorySerializer(serializers.ModelSerializer):
    # Only approved values are exposed; pending/rejected are review-queue state.
    values = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ["slug", "name", "applies_to", "description", "collapsed_limit", "values"]

    def get_values(self, obj: Category) -> list[dict]:
        qs = obj.values.filter(status=CategoryValue.STATUS_APPROVED)
        return CategoryValueSerializer(qs, many=True).data


class _BaseListingSerializer(serializers.ModelSerializer):
    vendor = VendorSerializer(read_only=True)
    validation_level_display = serializers.CharField(
        source="get_validation_level_display", read_only=True
    )
    compatibility = serializers.SerializerMethodField()

    class Meta:
        fields = [
            "slug", "name", "model_number", "description",
            "vendor", "validation_level", "validation_level_display",
            "attestation_count", "compatibility", "created_at", "updated_at",
        ]

    def get_compatibility(self, obj) -> list[dict]:
        """The AlmaLinux releases this listing is certified on, one per entry.

        The listing-level ``validation_level`` is the highest of these and
        ``attestation_count`` their total, so a client that wants to know whether a
        machine is still being validated on current releases has to read this list
        rather than the rollup - a vendor who certified 8 and stopped still shows a
        vendor badge.

        Majors only, the same unit the software catalog uses. Hardware rows used to carry a
        ``minimum_minor`` floor and publish "AlmaLinux 9.4+"; that field is gone. The minor a
        run passed on is still available per run, where it is provenance for the evidence
        rather than the scope of the claim.

        ``source`` separates a release a run proved from one that was merely declared,
        and it is the only field that does. A declared row may well carry a
        ``validation_level``: accepting a manual submission records a community
        attestation, which gives that release a community tier while nothing has
        actually been run on it. Read the two together. A client that treats a
        non-empty ``validation_level`` as "verified" will be wrong about every declared
        listing, which is why ``source`` is not optional to check.

        ``certifications`` and ``community_confirmations`` split the evidence the
        way the detail page does. Hardware keeps both kinds in one table told apart
        by tier, so without the split a client reading only ``attestation_count``
        would credit a vendor with the community's runs. ``attestation_count``
        keeps its existing meaning - every attestation on the release - rather than
        quietly becoming a different number.
        """
        rows = (
            obj.versions.select_related("release")
            # Both helpers walk the attestations, so fetch them once for the page
            # instead of twice per release.
            .prefetch_related("attestations")
            .annotate(confirmations=Count("attestations", distinct=True))
            .order_by("-release__major")
        )
        return [
            {
                "major": row.release.major,
                "display": row.display,
                # The timing gate. Null unless the evidence came from AlmaLinux Kitten and the
                # minor it anticipates has not shipped, so a consumer can present the same
                # "works from 10.3" note the catalog does. Derived, not stored: it clears when
                # an administrator records the minor as released.
                "pending_minor": row.pending_minor,
                "disclaimer": row.disclaimer,
                "validation_level": row.validation_level,
                "validation_level_display": (
                    row.get_validation_level_display() if row.validation_level else ""
                ),
                "source": row.source,
                # Who asserted it: vendor, almalinux, or both.
                "certifications": row.official_levels(),
                # What the community did, official runs excluded.
                "community_confirmations": row.community_confirmations(),
                "attestation_count": row.confirmations,
            }
            for row in rows
        ]


class SystemSerializer(_BaseListingSerializer):
    cpu_support = serializers.SerializerMethodField()

    class Meta(_BaseListingSerializer.Meta):
        model = System
        fields = _BaseListingSerializer.Meta.fields + ["cpu_support"]

    def get_cpu_support(self, obj: System) -> list[dict]:
        """CPU families, each flagged with whether a run actually proved it.

        A client checking whether a machine will take a given generation needs
        both halves, and needs to be able to tell them apart: a system may
        accept three generations with only one validated.
        """
        return [
            {
                "name": entry["cpu"].name,
                "vendor": entry["cpu"].vendor.name,
                "slug": entry["cpu"].slug,
                "validated": entry["validated"],
                "validation_level": (
                    entry["cpu"].validation_level if entry["validated"] else None
                ),
            }
            for entry in obj.cpu_support()
        ]


class ComponentSerializer(_BaseListingSerializer):
    used_in_systems = serializers.SerializerMethodField()

    class Meta(_BaseListingSerializer.Meta):
        model = Component
        fields = _BaseListingSerializer.Meta.fields + ["used_in_systems"]

    def get_used_in_systems(self, obj: Component) -> list[dict]:
        """Published systems this part is recorded in, and the nature of each
        link, so the catalog is navigable from either end."""
        return [
            {
                "name": entry["system"].name,
                "vendor": entry["system"].vendor.name,
                "slug": entry["system"].slug,
                "relation": entry["relation"],
                "validation_level": entry["system"].validation_level,
            }
            for entry in obj.used_in_systems()
        ]


# --- certification-suite results ---------------------------------------------


class TestResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestResult
        fields = ["test_id", "category", "severity", "status", "reason", "duration_ms",
                  "details"]


class BenchmarkResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = BenchmarkResult
        fields = ["benchmark_id", "benchmark_version", "category", "metric", "value",
                  "unit", "direction", "is_primary", "device_raw", "device_model",
                  "device_ordinal", "context"]


class TestRunSerializer(serializers.ModelSerializer):
    """Public view of an ingested run.

    Reviewer-only state (reviewer notes, who reviewed it) is deliberately
    absent, matching the convention in this module.
    """

    submitter = serializers.CharField(source="submitter.get_username", read_only=True)
    alma_release = serializers.IntegerField(source="alma_release.major", read_only=True)
    system = serializers.CharField(source="listing_system.slug", read_only=True,
                                   default=None)
    verdict = serializers.SerializerMethodField()
    # What the run is a claim *about*. Without these a consumer sees ``verdict: true`` beside
    # ``system_vendor: "Dell Inc."`` and ``system_product: "OptiPlex 3080"`` and has no field with
    # which to tell a whole-machine validation from a claim about one card that happened to be
    # measured in that chassis. ``system`` is already null on a scoped run, but null is what a
    # not-yet-linked machine run reads as too, so it distinguishes nothing.
    claim_scope = serializers.ListField(child=serializers.CharField(), read_only=True)
    claim_subject = serializers.CharField(read_only=True)

    class Meta:
        model = TestRun
        fields = [
            "uuid", "run_type", "target_type", "schema_version", "suite_version",
            "submitter", "system", "alma_release", "alma_minor",
            "claim_scope", "claim_subject",
            "cpu_model", "cpu_vendor", "cpu_cores", "memory_mb",
            "gpu_model", "gpu_driver",
            "system_kind", "system_vendor", "system_product",
            "board_vendor", "board_model",
            "pre_release", "started_at", "finished_at", "published_at", "verdict",
        ]

    def get_verdict(self, obj: TestRun) -> bool | None:
        return obj.verdict()


class TestRunDetailSerializer(TestRunSerializer):
    results = TestResultSerializer(many=True, read_only=True)
    benchmarks = BenchmarkResultSerializer(many=True, read_only=True)
    inventory = serializers.JSONField(read_only=True)
    environment = serializers.JSONField(read_only=True)
    # Lifted out of ``inventory`` so a consumer asking "does this machine have
    # avx512f" does not have to know that the answer lives at
    # ``inventory.cpus[0].flags``. Detail only, never on the list serializer: a
    # current x86 CPU advertises 150-200 flags, and repeating that per row would
    # multiply a page of results several times over for something nobody filters a
    # list on.
    cpu_flags = serializers.ReadOnlyField()
    cpu_flag_groups = serializers.ReadOnlyField()

    class Meta(TestRunSerializer.Meta):
        fields = TestRunSerializer.Meta.fields + [
            "cpu_flags", "cpu_flag_groups",
            "inventory", "environment", "results", "benchmarks",
        ]


class LeaderboardRowSerializer(serializers.ModelSerializer):
    run_uuid = serializers.UUIDField(source="run.uuid", read_only=True)
    cpu_model = serializers.CharField(source="run.cpu_model", read_only=True)
    gpu_model = serializers.CharField(source="run.gpu_model", read_only=True)
    gpu_driver = serializers.CharField(source="run.gpu_driver", read_only=True)
    system_vendor = serializers.CharField(source="run.system_vendor", read_only=True)
    system_product = serializers.CharField(source="run.system_product", read_only=True)
    alma_release = serializers.IntegerField(source="run.alma_release.major",
                                            read_only=True, default=None)
    published_at = serializers.DateTimeField(source="run.published_at", read_only=True)

    class Meta:
        model = BenchmarkResult
        fields = [
            "run_uuid", "benchmark_id", "benchmark_version", "metric", "value", "unit",
            "direction", "cpu_model", "gpu_model", "gpu_driver", "system_vendor",
            # device_model is the per-row GPU identity the leaderboard groups on; gpu_model (the
            # run's single card) stays for CPU rows and back-compat. device_raw/ordinal disambiguate
            # identical cards under --all-gpus. Blank/0 on non-GPU rows.
            "device_raw", "device_model", "device_ordinal",
            "system_product", "alma_release", "published_at",
        ]


class SoftwareSerializer(serializers.ModelSerializer):
    """A software product and its per-release validation.

    ``compatibility`` is the interesting part and the reason this is not a flat
    row: each AlmaLinux major a product cites carries its own tier and its own
    community confirmation count, so a consumer can tell "certified on 9,
    community-only on 10" from "certified on both".

    Built from ``.approved()`` only, mirroring how ``CategorySerializer`` filters
    values: a community-reported major awaiting review is review-queue state, and
    exposing it here while the HTML hides it would make the review gate
    decorative. Nothing about who attested or who submitted appears either.
    """

    vendor = VendorSerializer(read_only=True)
    validation_level_display = serializers.CharField(
        source="get_validation_level_display", read_only=True
    )
    compatibility = serializers.SerializerMethodField()

    class Meta:
        model = Software
        fields = [
            "slug", "name", "vendor", "description",
            "homepage_url", "documentation_url", "support_url",
            "validation_level", "validation_level_display",
            "compatibility", "created_at", "updated_at",
        ]

    def get_compatibility(self, obj: Software) -> list[dict]:
        rows = (
            obj.compatibility.approved()
            .select_related("release")
            .prefetch_related("certifications")
            .annotate(confirmations=Count("attestations", distinct=True))
        )
        return [
            {
                "major": row.release.major,
                "validation_level": row.validation_level,
                "validation_level_display": row.get_validation_level_display(),
                "certifications": [c.level for c in row.certifications.all()],
                "attestation_count": row.confirmations,
            }
            for row in rows
        ]
