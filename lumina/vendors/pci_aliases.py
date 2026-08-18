"""How pci.ids spells the silicon vendors, and what this catalog calls them.

The collectors report lspci's strings verbatim - deciding what a vendor is *called* belongs on the
server, where the alias table lives and where a wrong call can be corrected for every bundle ever
submitted. This is the table that makes those strings resolve.

Without it the failure is quiet and specific. Measured before writing it:

    NVIDIA Corporation                      -> NVIDIA      (normalization is enough)
    Intel Corporation                       -> Intel        (likewise)
    Advanced Micro Devices, Inc. [AMD/ATI]  -> None
    Realtek Semiconductor Co., Ltd.         -> None
    Broadcom Inc. and subsidiaries          -> None

``resolve_vendor`` returning None makes ``_vendor_for`` mint a *new* vendor under the long name,
so an AMD GPU is orphaned from the AMD that owns the curated CPU and GPU families and family
matching silently stops working for that manufacturer.

Read by two callers, which is why it is a module rather than a constant inside either: the
``vendors.0002`` data migration, for deployments that already have these vendors, and
``seed_devstack``, because on a fresh database the migration runs before anything has created
them. Both only ever add, and only where the target vendor already exists.

Deliberately short. A guessed mapping that is wrong merges two companies, which is worse than
leaving one unaliased and letting somebody notice a duplicate.
"""
from __future__ import annotations

# (pci.ids spelling, the catalog's name for the same company).
PCI_VENDOR_ALIASES: list[tuple[str, str]] = [
    ("NVIDIA Corporation", "NVIDIA"),
    ("Advanced Micro Devices, Inc. [AMD/ATI]", "AMD"),
    ("Intel Corporation", "Intel"),
    ("ASPEED Technology, Inc.", "ASPEED"),
    ("Matrox Electronics Systems Ltd.", "Matrox"),
    ("Realtek Semiconductor Co., Ltd.", "Realtek"),
    ("Broadcom Inc. and subsidiaries", "Broadcom"),
    ("Broadcom Limited", "Broadcom"),
    ("Mellanox Technologies", "Mellanox"),
    ("Marvell Technology Group Ltd.", "Marvell"),
    ("Aquantia Corp.", "Aquantia"),
]


def ensure(Vendor, VendorAlias) -> int:
    """Create any missing alias whose vendor exists. Returns how many were added.

    Takes the models as arguments so a migration can pass its historical versions.
    """
    added = 0
    for spelling, canonical in PCI_VENDOR_ALIASES:
        vendor = Vendor.objects.filter(name__iexact=canonical).first()
        if vendor is None:
            continue
        # ``name`` is unique across the table, so an alias somebody has already pointed
        # somewhere else is left exactly where it is.
        _, created = VendorAlias.objects.get_or_create(
            name=spelling, defaults={"vendor": vendor},
        )
        added += int(created)
    return added
