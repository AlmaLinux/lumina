"""Turn a survey bundle's inventory into SurveySubmission column values.

Mirrors ``results.inventory_extract.extract`` but for the survey's own columns,
reusing that module's substantive helpers (they pull in ``pci_names`` only,
never the catalog). It also keeps the raw identity fields the certification
extractor deliberately drops, and detects virtualization so the rollup can
exclude VMs.
"""
from __future__ import annotations

from lumina.results import inventory_extract as inv
from lumina.results.pci_names import gpu_identity
from lumina.survey import normalize

# The reliable virtual/physical signal is the CPU "hypervisor" flag. DMI
# product/vendor markers add a label and catch the rare case the flag is absent.
_VIRT_MARKERS = (
    ("vmware", "vmware"),
    ("virtualbox", "virtualbox"),
    ("kvm", "kvm"),
    ("qemu", "qemu"),
    ("bochs", "qemu"),
    ("xen", "xen"),
    ("hyper-v", "hyperv"),
    ("virtual machine", "hyperv"),
    ("amazon ec2", "aws"),
    ("google compute engine", "gce"),
    ("openstack", "openstack"),
    ("parallels", "parallels"),
    ("standard pc", "qemu"),
)


def _clean(value, max_length: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_length]


def _as_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def detect_virtualization(inventory: dict) -> tuple[bool, str]:
    cpu = inv.first_cpu(inventory)
    flags = {str(f).lower() for f in (cpu.get("flags") or [])}
    system = ((inventory or {}).get("summary") or {}).get("system") or {}
    haystack = f"{system.get('vendor') or ''} {system.get('product') or ''}".lower()
    kind = ""
    for needle, label in _VIRT_MARKERS:
        if needle in haystack:
            kind = label
            break
    virtual = "hypervisor" in flags or bool(kind)
    return virtual, (kind or ("unknown" if virtual else ""))


def _os(environment: dict) -> dict:
    return ((environment or {}).get("os") or {})


def _machine_id(inventory: dict, environment: dict) -> str:
    """``/etc/machine-id`` if the survey bundle carries it (environment or summary)."""
    return (
        (environment or {}).get("machine_id")
        or _os(environment).get("machine_id")
        or (((inventory or {}).get("summary") or {}).get("machine_id"))
        or ""
    )


def survey_extract(inventory: dict, environment: dict) -> dict:
    """Column values for a SurveySubmission - facets, raw identity, and virt state."""
    summary = (inventory or {}).get("summary") or {}
    system = summary.get("system") or {}
    baseboard = summary.get("baseboard") or {}
    memory = summary.get("memory") or {}
    gpus = summary.get("gpus") or []

    cpu = inv.first_cpu(inventory)
    gpu_vendor, gpu_name = gpu_identity(inv._primary_gpu(gpus))
    virtual, virt_kind = detect_virtualization(inventory)
    major, minor = inv.parse_version(environment)
    mem = inv._memory_summary(memory.get("dimms"))
    os = _os(environment)

    try:
        memory_bytes = int(memory.get("total_bytes")) or None
    except (TypeError, ValueError):
        memory_bytes = None

    return {
        # --- facets (normalized strings, no catalog FK) ---
        "cpu_model": _clean(normalize.cpu_model(cpu.get("model") or ""), 200),
        "cpu_vendor": _clean(normalize.cpu_vendor(cpu.get("vendor") or ""), 80),
        "cpu_sockets": _as_int(cpu.get("sockets")),
        "cpu_cores": _as_int(cpu.get("cores")),
        "cpu_threads": _as_int(cpu.get("threads")),
        "memory_bytes": memory_bytes,
        "memory_type": mem["memory_type"],
        "gpu_vendor": _clean(gpu_vendor, 80),
        "gpu_model": _clean(normalize.gpu_model(gpu_name or ""), 200),
        "board_vendor": _clean(baseboard.get("vendor"), 120),
        "board_model": _clean(normalize.board_model(baseboard.get("product") or ""), 200),
        "arch": _clean(os.get("arch"), 32),
        "x86_64_level": _clean(os.get("x86_64_level"), 8),
        "kernel": _clean(os.get("kernel") or (summary.get("drivers") or {}).get("kernel"), 120),
        "os_major": major,
        "os_minor": minor,
        # --- raw identity (access-controlled tier) ---
        "system_uuid": _clean(system.get("uuid"), 64),
        "system_serial": _clean(system.get("serial"), 128),
        "board_serial": _clean(baseboard.get("serial"), 128),
        "machine_id": _clean(_machine_id(inventory, environment), 64),
        # --- virt (bare-metal only; VMs kept but excluded at rollup) ---
        "virtual": virtual,
        "virt_kind": virt_kind,
    }
