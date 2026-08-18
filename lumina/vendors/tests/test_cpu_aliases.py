"""CPUID vendor strings, published as brands.

``lscpu`` reports what the silicon answers with: "GenuineIntel", "AuthenticAMD". Those are
identifiers, not names, and nobody writes them, so a page that shows a person the answer
should say Intel and AMD.

The table lives in the vendors app rather than in either consumer, because two consumers
need it and disagreeing is the failure: the catalog names a CPU component "Intel" through
``results.services.cpu_brand`` while the hardware survey published "GenuineIntel" for the
same machine.
"""
from __future__ import annotations

import pytest

from lumina.vendors.cpu_aliases import brand


@pytest.mark.parametrize("reported,expected", [
    ("GenuineIntel", "Intel"),
    ("AuthenticAMD", "AMD"),
    ("HygonGenuine", "Hygon"),
    ("CentaurHauls", "VIA"),
    ("ARM", "Arm"),
    ("APM", "Ampere"),
])
def test_a_known_cpuid_string_becomes_a_brand(reported, expected):
    assert brand(reported) == expected


def test_matching_ignores_case_and_padding():
    # Zhaoxin pads its CPUID string with spaces, and lscpu passes it through.
    assert brand("  Shanghai  ") == "Zhaoxin"
    assert brand("genuineintel") == "Intel"


def test_an_arm_firmware_vendor_is_already_a_brand_and_passes_through():
    # The suite reads the silicon vendor from the BIOS fields on aarch64, because MIDR
    # only names the architecture licensor, so these arrive as names already.
    assert brand("Ampere") == "Ampere"


def test_an_unmapped_vendor_is_shown_as_reported():
    # Ugly and honest, and the signal that an entry is wanted. Guessing would risk
    # merging two companies, which is the failure the table's own comment warns about.
    assert brand("Some New Silicon Co.") == "Some New Silicon Co."
    assert brand("") == ""
    assert brand(None) == ""
