"""Why one survey submission does, or does not, appear in the published statistics.

A machine can be missing from the statistics for half a dozen unrelated reasons - it was
dismissed, it was detected as a VM, an exclusion rule matches its card, its payload
carries no device enumeration, the rollup has not run since it arrived - and every one of
them looks identical from the statistics page: an absence. The submission's own review
page made it worse, because it listed a card the rollup had skipped: the page shows what
the machine reported, the rollup counts what qualifies.

So this walks the gates the rollup applies, in order, and reports the answer at each one.
Structured rather than printed, because two callers render it: the reviewer's page (where
the question is actually asked) and ``manage.py survey_explain`` (for a shell). Building
it once is the point - two explanations of one pipeline would eventually disagree, and a
diagnostic that names the wrong gate is worse than none.

Read-only. Nothing here writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from lumina.results.device_inventory import categorize_inventory
from lumina.results.exclusions import active_rules
from lumina.results.pci_names import gpu_identity, pci_device_id, pci_vendor_id
from lumina.survey.devices import census_exclusion_reason, countable_gpus
from lumina.survey.models import SurveyStat, SurveySubmission
from lumina.vendors import cpu_aliases


@dataclass
class Check:
    """One gate, and whether this submission passes it."""

    label: str
    ok: bool
    detail: str


@dataclass
class DeviceLine:
    """One reported graphics device, and whether the census counts it."""

    label: str
    ids: str
    counted: bool
    reason: str = ""


@dataclass
class Explanation:
    checks: list[Check] = field(default_factory=list)
    devices: list[DeviceLine] = field(default_factory=list)
    rules: list = field(default_factory=list)
    gpus: list[str] = field(default_factory=list)
    cpu: str = ""
    period: str = ""
    published_rows: int = 0
    gpu_buckets: list[str] = field(default_factory=list)
    verdict_ok: bool = False
    verdict: str = ""

    @property
    def counts(self) -> bool:
        """Whether the submission is eligible to appear at all."""
        return all(check.ok for check in self.checks)


def explain(sub: SurveySubmission, rules: list | None = None) -> Explanation:
    """The gate-by-gate account of one submission's path into the statistics."""
    rules = active_rules() if rules is None else rules
    out = Explanation(rules=list(rules))

    # --- eligibility: either of these keeps the machine out of every statistic ---
    dismissed = sub.review_state == SurveySubmission.REVIEW_DISMISSED
    out.checks.append(Check(
        label="Moderation",
        ok=not dismissed,
        detail=(
            "Dismissed by a reviewer, so it counts nowhere. Accept it to bring it back."
            if dismissed else
            f"{sub.get_review_state_display()}, which counts: review is oversight, not a gate."
        ),
    ))
    out.checks.append(Check(
        label="Bare metal",
        ok=not sub.virtual,
        detail=(
            f"Detected as a virtual machine ({sub.virt_kind or 'kind unknown'}). The survey "
            "is bare metal only, so it counts nowhere."
            if sub.virtual else
            "Not detected as a virtual machine."
        ),
    ))

    # --- what the payload actually carries ---
    summary = (sub.inventory or {}).get("summary") or {}
    enumerated = categorize_inventory(sub.inventory or {})["gpus"]
    out.checks.append(Check(
        label="Device enumeration",
        ok=bool(enumerated),
        detail=(
            f"{len(summary.get('pci_devices') or [])} PCI device(s) enumerated and "
            f"{len(summary.get('gpus') or [])} in the collector's own list, giving "
            f"{len(enumerated)} graphics device(s)."
            if enumerated else
            "This report carries no graphics device at all. That is a collector-side "
            "result on the machine, not a decision made here."
        ),
    ))

    for dev in enumerated:
        vendor, model = gpu_identity(dev)
        reason = census_exclusion_reason(dev, rules)
        out.devices.append(DeviceLine(
            label=f"{vendor} {model}".strip() or "(unnamed)",
            ids=f"{pci_vendor_id(dev) or '????'}:{pci_device_id(dev) or '????'}",
            counted=reason is None,
            reason=reason or "",
        ))

    counted = countable_gpus(sub.inventory, rules)
    if counted is None:
        out.gpus = []
        gpu_note = ("No device enumeration to judge, so the column stored at ingest is "
                    "used as it is.")
    elif not counted:
        out.gpus = []
        gpu_note = ("Every graphics device above was skipped, so this machine counts "
                    "under no GPU vendor.")
    else:
        # Every one, because the statistics count every one: a machine with an integrated
        # display and a discrete card contributes both.
        out.gpus = [vendor for vendor, _ in counted if vendor]
        listed = ", ".join(f"{vendor} ({model})" for vendor, model in counted)
        gpu_note = f"Counts under {len(counted)} GPU(s): {listed}."
    out.checks.append(Check(label="GPU counted", ok=bool(out.gpus), detail=gpu_note))

    out.cpu = cpu_aliases.brand(sub.cpu_vendor)

    # --- has the rollup caught up? ---
    out.period = timezone.localtime(sub.received_at).strftime("%Y-%m")
    published = SurveyStat.objects.filter(period=out.period, segment="")
    out.published_rows = published.count()
    out.gpu_buckets = sorted(
        published.filter(dimension="gpu_vendor").values_list("bucket", flat=True).distinct()
    )

    if not out.counts:
        out.verdict_ok = False
        out.verdict = ("This submission contributes nothing to the statistics. See the "
                       "failed check above.")
    elif not out.published_rows:
        out.verdict_ok = False
        out.verdict = (
            f"Nothing is published for {out.period} yet, so the rollup has not run since "
            "this arrived. Rebuild it from the review queue."
        )
    elif missing := [vendor for vendor in out.gpus if vendor not in out.gpu_buckets]:
        out.verdict_ok = False
        out.verdict = (
            f"It should count under {', '.join(missing)}, but no such row is published "
            f"for {out.period}: the rollup last ran before this was derivable. Rebuild it."
        )
    else:
        out.verdict_ok = True
        out.verdict = "Counted, and the published figures agree."
    return out
