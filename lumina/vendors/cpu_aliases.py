"""What a CPU calls its maker, and what this catalog calls the same company.

``lscpu`` reports the CPUID vendor string, which is a 12-byte identifier the silicon
answers with and not a brand: "GenuineIntel", "AuthenticAMD". Nobody writes those, so
anywhere a person reads the answer it should say Intel and AMD.

Sits beside ``pci_aliases`` because it answers the same question for a different bus, and
in the vendors app because naming a company is that app's job. Kept as data plus one pure
function so every consumer shares one answer: the catalog's component ties (which decide
what a CPU component is called) and the published hardware survey. Those two disagreeing
would put "Intel" in the catalog and "GenuineIntel" in the statistics for one machine.

Deliberately short, on the same principle as ``pci_aliases``: an entry that is wrong
merges two companies, which is worse than one ugly string nobody has mapped yet.
"""
from __future__ import annotations

# Keyed by the reported string, casefolded.
CPU_VENDOR_NAMES = {
    "authenticamd": "AMD",
    "genuineintel": "Intel",
    "arm": "Arm",
    "apm": "Ampere",
    # Arm servers report the silicon vendor from the firmware rather than from MIDR (see
    # the suite's inventory/cpu.py), so these arrive as brands already and need no entry.
    # The rest are the CPUID strings for parts that turn up in servers occasionally.
    "hygongenuine": "Hygon",
    "centaurhauls": "VIA",
    "  shanghai  ": "Zhaoxin",
    "shanghai": "Zhaoxin",
}


def brand(vendor: str) -> str:
    """The brand name for a reported CPU vendor string, or the string itself.

    Falls through rather than guessing: an unmapped vendor is shown as reported, which is
    ugly and honest, and is the signal that a new entry is wanted here.
    """
    return CPU_VENDOR_NAMES.get((vendor or "").strip().casefold(), vendor or "")
