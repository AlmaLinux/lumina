"""Catalog-free normalization of raw hardware model strings.

Lifted deliberately from ``results.component_match`` so the survey app never
imports the certification catalog: the census shares the collector, never the
catalog. These are the pure, kind-specific string reducers only - the matching
and component-creation half stays in ``component_match``. Keep the regexes in
step with that module if they change there.
"""
from __future__ import annotations

import re

_MARKS_RE = re.compile(r"\((?:R|TM|C)\)|[®™©]", re.I)
_CLOCK_RE = re.compile(r"@\s*[\d.]+\s*[GM]Hz", re.I)
_NCORE_RE = re.compile(r"\b\d+-Cores?\s+Processor\b", re.I)
_CPU_WORD_RE = re.compile(r"\bCPU\b", re.I)
_PROC_WORD_RE = re.compile(r"\bProcessors?\b", re.I)
_ZERO_RE = re.compile(r"\b0\b(?:\s*@.*)?$")
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")


def cpu_model(model: str) -> str:
    """"Intel(R) Xeon(R) Gold 6430" -> "Intel Xeon Gold 6430"."""
    s = model or ""
    for pattern in (_MARKS_RE, _CLOCK_RE, _NCORE_RE, _CPU_WORD_RE, _PROC_WORD_RE, _ZERO_RE):
        s = pattern.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def cpu_vendor(vendor: str) -> str:
    """Tidy a raw CPU vendor string without changing which vendor it names.

    "Ampere(R)" -> "Ampere". x86_64 vendors arrive as the MIDR-style keys
    "GenuineIntel" and "AuthenticAMD" and pass through untouched, which keeps them
    usable as the grouping key they already are. The marks only ever show up on Arm,
    where the collector reads the vendor out of the BIOS fields because lscpu's
    "Vendor ID" there is the architecture licensor (ARM) rather than who built the chip.
    """
    return re.sub(r"\s+", " ", _MARKS_RE.sub(" ", vendor or "")).strip(" -")


def gpu_model(model: str) -> str:
    """"AD102 [GeForce RTX 4090]" -> "GeForce RTX 4090"."""
    s = model or ""
    bracket = _BRACKET_RE.search(s)
    if bracket:
        s = bracket.group(1)
    s = _MARKS_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def board_model(model: str) -> str:
    """Boards are comparatively clean: strip trademark marks and collapse whitespace."""
    s = _MARKS_RE.sub(" ", model or "")
    return re.sub(r"\s+", " ", s).strip(" -")
