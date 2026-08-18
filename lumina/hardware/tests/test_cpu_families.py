"""The seeded CPU families, checked against real reported model strings.

The starting set ships in a data migration, so it is present in every
database - including this test database. A wrong pattern silently
mis-certifies hardware, so every family is pinned with strings as the tools
actually report them (lscpu/DMI wording included), plus the near-miss cases
where a sloppy pattern would bleed across generations.
"""
from __future__ import annotations

import pytest

from lumina.hardware.models import Component, ComponentKind, ComponentRole
from lumina.results.component_match import family_for_model

pytestmark = pytest.mark.django_db


def resolve(model: str) -> str | None:
    family = family_for_model(model, ComponentKind.cpu)
    return family.name if family else None


# --- AMD EPYC: the fourth digit is the generation -----------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("AMD EPYC 7601 32-Core Processor", "AMD EPYC 7001 Series"),
        ("AMD EPYC 7742 64-Core Processor", "AMD EPYC 7002 Series"),
        ("AMD EPYC 7232P 8-Core Processor", "AMD EPYC 7002 Series"),
        ("AMD EPYC 7763 64-Core Processor", "AMD EPYC 7003 Series"),
        # Milan-X carries a 3D V-Cache X suffix
        ("AMD EPYC 7773X 64-Core Processor", "AMD EPYC 7003 Series"),
        ("AMD EPYC 9354 32-Core Processor", "AMD EPYC 9004 Series"),
        ("AMD EPYC 9474F 48-Core Processor", "AMD EPYC 9004 Series"),
        # Bergamo shares the 9004 series numbering
        ("AMD EPYC 9754 128-Core Processor", "AMD EPYC 9004 Series"),
        ("AMD EPYC 9684X 96-Core Processor", "AMD EPYC 9004 Series"),
        ("AMD EPYC 9755 128-Core Processor", "AMD EPYC 9005 Series"),
        ("AMD EPYC 9575F 64-Core Processor", "AMD EPYC 9005 Series"),
        ("AMD EPYC 8534P 64-Core Processor", "AMD EPYC 8004 Series"),
        ("AMD EPYC 4564P 16-Core Processor", "AMD EPYC 4004 Series"),
        ("AMD EPYC 3251 8-Core Processor", "AMD EPYC 3000 Series"),
    ],
)
def test_epyc_generations(model, expected):
    assert resolve(model) == expected


def test_epyc_generations_do_not_bleed():
    """9004 and 9005 differ by one digit; conflating them would certify
    Turin hardware as Genoa."""
    assert resolve("AMD EPYC 9355 32-Core Processor") == "AMD EPYC 9005 Series"
    assert resolve("AMD EPYC 9354 32-Core Processor") == "AMD EPYC 9004 Series"


# --- AMD Ryzen: first digit of the model is the series ------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("AMD Ryzen 5 1600 Six-Core Processor", "AMD Ryzen 1000 Series"),
        ("AMD Ryzen 7 2700X Eight-Core Processor", "AMD Ryzen 2000 Series"),
        ("AMD Ryzen 9 3950X 16-Core Processor", "AMD Ryzen 3000 Series"),
        ("AMD Ryzen 7 4750G with Radeon Graphics", "AMD Ryzen 4000 Series"),
        ("AMD Ryzen 9 5950X 16-Core Processor", "AMD Ryzen 5000 Series"),
        ("AMD Ryzen 7 5800X3D 8-Core Processor", "AMD Ryzen 5000 Series"),
        ("AMD Ryzen 7 PRO 5750G", "AMD Ryzen 5000 Series"),
        ("AMD Ryzen 9 7950X 16-Core Processor", "AMD Ryzen 7000 Series"),
        ("AMD Ryzen 7 7800X3D 8-Core Processor", "AMD Ryzen 7000 Series"),
        ("AMD Ryzen 5 8600G w/ Radeon 760M Graphics", "AMD Ryzen 8000 Series"),
        ("AMD Ryzen 9 9950X 16-Core Processor", "AMD Ryzen 9000 Series"),
        ("AMD Ryzen 7 9800X3D 8-Core Processor", "AMD Ryzen 9000 Series"),
    ],
)
def test_ryzen_generations(model, expected):
    assert resolve(model) == expected


@pytest.mark.parametrize(
    "model,expected",
    [
        ("AMD Ryzen Threadripper 1950X 16-Core Processor",
         "AMD Ryzen Threadripper 1000 Series"),
        ("AMD Ryzen Threadripper 2990WX 32-Core Processor",
         "AMD Ryzen Threadripper 2000 Series"),
        ("AMD Ryzen Threadripper 3990X 64-Core Processor",
         "AMD Ryzen Threadripper 3000 Series"),
        ("AMD Ryzen Threadripper PRO 5995WX 64-Cores",
         "AMD Ryzen Threadripper 5000 Series"),
        ("AMD Ryzen Threadripper PRO 7995WX 96-Cores",
         "AMD Ryzen Threadripper 7000 Series"),
    ],
)
def test_threadripper_generations(model, expected):
    assert resolve(model) == expected


def test_threadripper_is_not_mistaken_for_a_ryzen_desktop_part():
    """"Ryzen 9 3950X" and "Ryzen Threadripper 3990X" are different families
    despite both being 3000-series AMD parts."""
    assert resolve("AMD Ryzen Threadripper 3990X 64-Core Processor") == \
        "AMD Ryzen Threadripper 3000 Series"
    assert resolve("AMD Ryzen 9 3950X 16-Core Processor") == "AMD Ryzen 3000 Series"


# --- Intel Core: generation is the leading digit(s) ---------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("Intel(R) Core(TM) i7-2600K CPU @ 3.40GHz", "Intel Core 2nd Generation"),
        ("Intel(R) Core(TM) i5-4590 CPU @ 3.30GHz", "Intel Core 4th Generation"),
        ("Intel(R) Core(TM) i7-6700K CPU @ 4.00GHz", "Intel Core 6th Generation"),
        ("Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz", "Intel Core 8th Generation"),
        ("Intel(R) Core(TM) i9-9900K CPU @ 3.60GHz", "Intel Core 9th Generation"),
        ("Intel(R) Core(TM) i9-10900K CPU @ 3.70GHz", "Intel Core 10th Generation"),
        # 10th-gen mobile Ice Lake uses a four-digit model
        ("Intel(R) Core(TM) i7-1065G7 CPU @ 1.30GHz", "Intel Core 10th Generation"),
        ("Intel(R) Core(TM) i5-11400 @ 2.60GHz", "Intel Core 11th Generation"),
        ("Intel(R) Core(TM) i9-12900K", "Intel Core 12th Generation"),
        ("Intel(R) Core(TM) i7-13700H", "Intel Core 13th Generation"),
        ("Intel(R) Core(TM) i9-14900KS", "Intel Core 14th Generation"),
        ("Intel(R) Core(TM) i3-10100T CPU @ 3.00GHz", "Intel Core 10th Generation"),
        ("Intel(R) Core(TM) Ultra 7 155H", "Intel Core Ultra Series 1"),
        ("Intel(R) Core(TM) Ultra 9 285K", "Intel Core Ultra Series 2"),
    ],
)
def test_intel_core_generations(model, expected):
    assert resolve(model) == expected


def test_four_digit_generation_does_not_swallow_a_five_digit_model():
    """Without the digit-count guard, the 1st-gen-style pattern for "1xxx"
    would also match 10th-14th generation five-digit models."""
    assert resolve("Intel(R) Core(TM) i9-14900K") == "Intel Core 14th Generation"
    assert resolve("Intel(R) Core(TM) i9-9900K") == "Intel Core 9th Generation"
    # a 3-digit 1st-gen part matches no seeded family rather than a wrong one
    assert resolve("Intel(R) Core(TM) i7-870 CPU @ 2.93GHz") is None


# --- Intel Xeon ---------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("Intel(R) Xeon(R) E3-1270 v6 @ 3.80GHz",
         "Intel Xeon E3 v6 Family (Kaby Lake)"),
        ("Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz",
         "Intel Xeon E5 v4 Family (Broadwell-EP)"),
        ("Intel(R) Xeon(R) CPU E7-8890 v4 @ 2.20GHz",
         "Intel Xeon E7 v4 Family (Broadwell-EX)"),
        ("Intel(R) Xeon(R) E-2288G CPU @ 3.70GHz", "Intel Xeon E Family"),
        ("Intel(R) Xeon(R) D-2146NT CPU @ 2.30GHz", "Intel Xeon D Family"),
        ("Intel(R) Xeon(R) W-2295 CPU @ 3.00GHz", "Intel Xeon W Family"),
        ("Intel(R) Xeon(R) w9-3495X", "Intel Xeon W Family"),
        ("Intel(R) Xeon(R) 6980P", "Intel Xeon 6"),
    ],
)
def test_intel_xeon_families(model, expected):
    assert resolve(model) == expected


# --- Xeon Scalable: the second digit of the model is the generation ----------


@pytest.mark.parametrize(
    "model,expected",
    [
        # 1st gen Skylake-SP
        ("Intel(R) Xeon(R) Platinum 8180 CPU @ 2.50GHz",
         "Intel Xeon Scalable 1st Generation"),
        ("Intel(R) Xeon(R) Gold 6148 CPU @ 2.40GHz",
         "Intel Xeon Scalable 1st Generation"),
        ("Intel(R) Xeon(R) Silver 4114 CPU @ 2.20GHz",
         "Intel Xeon Scalable 1st Generation"),
        ("Intel(R) Xeon(R) Bronze 3106 CPU @ 1.70GHz",
         "Intel Xeon Scalable 1st Generation"),
        # 2nd gen Cascade Lake
        ("Intel(R) Xeon(R) Platinum 8280 CPU @ 2.70GHz",
         "Intel Xeon Scalable 2nd Generation"),
        ("Intel(R) Xeon(R) Gold 6248 CPU @ 2.50GHz",
         "Intel Xeon Scalable 2nd Generation"),
        ("Intel(R) Xeon(R) Silver 4214 CPU @ 2.20GHz",
         "Intel Xeon Scalable 2nd Generation"),
        ("Intel(R) Xeon(R) Bronze 3204 CPU @ 1.90GHz",
         "Intel Xeon Scalable 2nd Generation"),
        # Cascade Lake-AP uses a 9xxx tier digit
        ("Intel(R) Xeon(R) Platinum 9242 CPU @ 2.30GHz",
         "Intel Xeon Scalable 2nd Generation"),
        # 3rd gen Ice Lake-SP and Cooper Lake
        ("Intel(R) Xeon(R) Platinum 8380 CPU @ 2.30GHz",
         "Intel Xeon Scalable 3rd Generation"),
        ("Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz",
         "Intel Xeon Scalable 3rd Generation"),
        ("Intel(R) Xeon(R) Gold 5320H CPU @ 2.40GHz",
         "Intel Xeon Scalable 3rd Generation"),
        # 4th gen Sapphire Rapids
        ("Intel(R) Xeon(R) Gold 6430", "Intel Xeon Scalable 4th Generation"),
        ("Intel(R) Xeon(R) Platinum 8480+",
         "Intel Xeon Scalable 4th Generation"),
        ("Intel(R) Xeon(R) Silver 4410Y",
         "Intel Xeon Scalable 4th Generation"),
        ("Intel(R) Xeon(R) Bronze 3408U",
         "Intel Xeon Scalable 4th Generation"),
        # 5th gen Emerald Rapids
        ("Intel(R) Xeon(R) Platinum 8592+",
         "Intel Xeon Scalable 5th Generation"),
        ("Intel(R) Xeon(R) Gold 6548Y+",
         "Intel Xeon Scalable 5th Generation"),
        ("Intel(R) Xeon(R) Gold 5520+",
         "Intel Xeon Scalable 5th Generation"),
    ],
)
def test_xeon_scalable_generations(model, expected):
    assert resolve(model) == expected


def test_same_tier_across_generations_is_split():
    """A Gold 6248 and a Gold 6430 are different platforms; grouping them by
    tier would have hidden that."""
    assert resolve("Intel(R) Xeon(R) Gold 6248 CPU @ 2.50GHz") == \
        "Intel Xeon Scalable 2nd Generation"
    assert resolve("Intel(R) Xeon(R) Gold 6430") == \
        "Intel Xeon Scalable 4th Generation"


def test_scalable_does_not_collide_with_xeon_6():
    """"Xeon Gold 6248" must not fall into the "Xeon 6" family just because
    its model number starts with a 6, and a real Xeon 6 must not land in a
    Scalable generation."""
    assert resolve("Intel(R) Xeon(R) Gold 6248 CPU @ 2.50GHz") == \
        "Intel Xeon Scalable 2nd Generation"
    assert resolve("Intel(R) Xeon(R) 6980P") == "Intel Xeon 6"


def test_no_two_seeded_families_match_the_same_model():
    """Overlapping patterns would make resolution order-dependent."""
    from lumina.hardware.models import ComponentRole
    from lumina.results.component_match import matches_family

    samples = [
        "Intel(R) Xeon(R) Gold 6430", "Intel(R) Xeon(R) 6980P",
        "Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz",
        "Intel(R) Core(TM) i9-14900K", "AMD EPYC 9354 32-Core Processor",
        "AMD Ryzen 9 7950X 16-Core Processor",
        "AMD Ryzen Threadripper PRO 7995WX 96-Cores",
    ]
    families = Component.objects.filter(
        role=ComponentRole.FAMILY, kind=ComponentKind.cpu.value
    )
    for model in samples:
        hits = [fam.name for fam in families if matches_family(fam, model)]
        assert len(hits) == 1, f"{model} matched {hits}"


# --- the seeded set itself -----------------------------------------------------


def test_migration_seeds_families_unpublished():
    total = Component.objects.filter(
        kind=ComponentKind.cpu.value, role=ComponentRole.FAMILY
    ).count()
    assert total > 40
    # nothing appears publicly until real evidence attests it
    assert not Component.objects.filter(
        role=ComponentRole.FAMILY, published=True
    ).exists()


def test_seeded_vendors_are_not_duplicated():
    from lumina.vendors.models import Vendor

    assert Vendor.objects.filter(name__iexact="AMD").count() == 1
    assert Vendor.objects.filter(name__iexact="Intel").count() == 1


def test_every_seeded_pattern_compiles():
    """A bad regex in the seed data makes its family match nothing, silently.

    Reads the patterns from the database rather than from the migration that put them
    there. That is both more durable - the migration history has since been collapsed
    into one initial, so there is no ``0017`` to import - and a better test: what
    classification actually uses is the stored row, and a pattern that compiles in the
    source but was mangled on the way in would have passed the old version.
    """
    import re

    patterns = Component.objects.filter(role=ComponentRole.FAMILY).values_list(
        "name", "model_patterns"
    )
    assert patterns, "no families are seeded, so this test proves nothing"
    for name, entries in patterns:
        for pattern in entries:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise AssertionError(
                    f"{name} carries an uncompilable pattern {pattern!r}: {exc}"
                ) from exc


def test_seeded_families_all_carry_patterns():
    """A family with no patterns matches nothing, which would be a silent
    dead entry in the catalog."""
    empty = Component.objects.filter(
        role=ComponentRole.FAMILY, model_patterns=[]
    ).values_list("name", flat=True)
    assert list(empty) == []


# --- Xeon E3/E5/E7 versions ----------------------------------------------------

# Intel reused every model number across four or more generations, so the
# version is the platform boundary. The v1 parts carry no suffix at all, and
# many Sandy Bridge-EP chips report a bare trailing zero where the version
# would go - both forms have to land in v1 without dragging the later
# generations in with them.
@pytest.mark.parametrize(
    "model,expected",
    [
        ("Intel(R) Xeon(R) CPU E3-1220 @ 3.10GHz",
         "Intel Xeon E3 v1 Family (Sandy Bridge)"),
        # Case varies in the wild: some report "V2", some "v2".
        ("Intel(R) Xeon(R) CPU E3-1230 V2 @ 3.30GHz",
         "Intel Xeon E3 v2 Family (Ivy Bridge)"),
        ("Intel(R) Xeon(R) CPU E3-1231 v3 @ 3.40GHz",
         "Intel Xeon E3 v3 Family (Haswell)"),
        ("Intel(R) Xeon(R) CPU E3-1240 v5 @ 3.50GHz",
         "Intel Xeon E3 v5 Family (Skylake)"),
        ("Intel(R) Xeon(R) CPU E3-1220 v6 @ 3.00GHz",
         "Intel Xeon E3 v6 Family (Kaby Lake)"),
        # The trailing-zero quirk.
        ("Intel(R) Xeon(R) CPU E5-2670 0 @ 2.60GHz",
         "Intel Xeon E5 v1 Family (Sandy Bridge-EP)"),
        ("Intel(R) Xeon(R) CPU E5-2670 @ 2.60GHz",
         "Intel Xeon E5 v1 Family (Sandy Bridge-EP)"),
        ("Intel(R) Xeon(R) CPU E5-2670 v2 @ 2.50GHz",
         "Intel Xeon E5 v2 Family (Ivy Bridge-EP)"),
        ("Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz",
         "Intel Xeon E5 v4 Family (Broadwell-EP)"),
        ("Intel(R) Xeon(R) CPU E7-4870 @ 2.40GHz",
         "Intel Xeon E7 v1 Family (Westmere-EX)"),
        ("Intel(R) Xeon(R) CPU E7-8890 v3 @ 2.50GHz",
         "Intel Xeon E7 v3 Family (Haswell-EX)"),
    ],
)
def test_xeon_e_versions(model, expected):
    assert resolve(model) == expected


def test_the_same_model_number_across_versions_lands_apart():
    """The point of the split: an E5-2670 and an E5-2670 v3 are a Sandy
    Bridge-EP and a Haswell-EP, with different sockets, chipsets, and memory."""
    v1 = resolve("Intel(R) Xeon(R) CPU E5-2670 0 @ 2.60GHz")
    v3 = resolve("Intel(R) Xeon(R) CPU E5-2670 v3 @ 2.30GHz")
    assert v1 != v3


def test_the_broad_per_line_families_are_gone():
    """Left in place they would compete: matching returns the first family whose
    pattern hits and families() has no deterministic order, so a broad
    "Xeon E5-[0-9]{4}" would make classification depend on row order."""
    broad = Component.objects.filter(
        kind=ComponentKind.cpu.value,
        role=ComponentRole.FAMILY,
        name__in=["Intel Xeon E3 Family", "Intel Xeon E5 Family",
                  "Intel Xeon E7 Family"],
    ).exclude(model_patterns=[])
    assert not broad.exists()


def test_no_xeon_e9_family_was_invented():
    """There is no Xeon E9 line."""
    assert not Component.objects.filter(
        kind=ComponentKind.cpu.value, name__icontains="Xeon E9"
    ).exists()


def test_the_xeon_e_2000_line_is_untouched():
    """"Xeon E-2336" is a different line from "Xeon E3-1220" and must not be
    caught by the E3 patterns."""
    assert resolve("Intel(R) Xeon(R) E-2336 CPU @ 2.90GHz") == "Intel Xeon E Family"


def test_the_role_field_is_editable_in_the_admin():
    """It is validated against the patterns by ComponentAdminForm.clean, so
    leaving it out of the fieldsets meant every save of a family posted no role
    and failed that validation - the patterns were uneditable through the admin
    entirely, which is the only place they are meant to be curated."""
    from django.contrib import admin as dj

    from lumina.hardware.models import Component

    fieldsets = dj.site._registry[Component].fieldsets
    rendered = {field for _, opts in fieldsets for field in opts["fields"]}

    assert "role" in rendered
    assert "model_patterns" in rendered
    # Every field the form declares has to appear somewhere, or the same class
    # of bug comes back with a different field.
    from lumina.hardware.admin import ComponentAdminForm

    declared = set(ComponentAdminForm.Meta.fields)
    assert declared <= rendered, declared - rendered


def test_a_family_can_be_saved_through_the_admin(client):
    """End to end, because the form validated in isolation and still failed in
    the admin."""
    from django.contrib.auth.models import User

    from lumina.hardware.models import Component, ComponentRole

    admin_user = User.objects.create_superuser("fam-admin", "fa@example.com", "x")
    client.force_login(admin_user)
    family = Component.objects.filter(role=ComponentRole.FAMILY).first()

    response = client.post(
        f"/admin/hardware/component/{family.pk}/change/",
        {
            "name": family.name, "vendor": str(family.vendor_id),
            "kind": family.kind, "role": ComponentRole.FAMILY,
            "slug": family.slug, "model_patterns": r"Xeon TestPattern [0-9]{4}",
            "model_number": "", "description": "", "vendor_spec_url": "",
            "published": "", "validation_level": family.validation_level,
            "attestation_count": family.attestation_count, "attributes": "{}",
            "category_values-TOTAL_FORMS": "0",
            "category_values-INITIAL_FORMS": "0",
            "versions-TOTAL_FORMS": "0", "versions-INITIAL_FORMS": "0",
        },
    )

    assert response.status_code == 302, response.content.decode()[:400]
    family.refresh_from_db()
    assert family.model_patterns == [r"Xeon TestPattern [0-9]{4}"]
    assert family.role == ComponentRole.FAMILY
