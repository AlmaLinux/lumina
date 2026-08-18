"""Deriving a stable, non-reversible machine identity from raw signals.

The raw identifiers (SMBIOS UUID, serials, machine-id) are kept on the
submission; this turns the strongest usable one into an HMAC the rollup dedups
on. It is derived and recomputable: if the bogus-UUID denylist or the pepper
changes, re-run over the retained raw fields.
"""
from __future__ import annotations

import hashlib
import hmac

from lumina.results.inventory_extract import is_placeholder

# Valid-looking UUIDs firmware ships that identify nothing: all-zeros, all-Fs,
# and the batch defaults vendors leave in place (AMI, some Dell/QEMU images). A
# UUID here is skipped in favor of the next signal. Data-detected duplicates -
# one UUID across many boards - are handled at rollup time, not here.
BOGUS_UUIDS = frozenset({
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "03000200-0400-0500-0006-000700080009",
    "00020003-0004-0005-0006-000700080009",
    "12345678-1234-5678-1234-567812345678",
    "4c4c4544-0000-0000-0000-000000000000",
})

# Strongest to weakest. Each: (source label, denylist of unusable values).
_SOURCES = (
    ("smbios_uuid", BOGUS_UUIDS),
    ("board_serial", frozenset()),
    ("machine_id", frozenset()),
)


def _usable(value: str, denylist: frozenset) -> str:
    v = (value or "").strip()
    if not v or is_placeholder(v):
        return ""
    if v.lower() in denylist:
        return ""
    return v


def hash_identity(*, pepper: str, smbios_uuid: str = "", board_serial: str = "",
                  machine_id: str = "") -> tuple[str, str]:
    """Return ``(identity_hash, source)`` from the strongest usable signal, or ``("", "")``.

    The source label is folded into the hashed input, so a serial that happens to
    equal a UUID on another machine cannot collide with it.
    """
    values = {
        "smbios_uuid": smbios_uuid,
        "board_serial": board_serial,
        "machine_id": machine_id,
    }
    for source, denylist in _SOURCES:
        usable = _usable(values[source], denylist)
        if usable:
            digest = hmac.new(
                pepper.encode("utf-8"),
                f"{source}:{usable}".encode(),
                hashlib.sha256,
            ).hexdigest()
            return digest, source
    return "", ""
