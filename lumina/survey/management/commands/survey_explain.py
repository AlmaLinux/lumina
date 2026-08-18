"""Print why a survey submission is or is not in the published statistics.

The reasoning lives in ``lumina.survey.explain``, which the reviewer's own page renders
too: the page is where the question is normally asked, and this is the same account for a
shell. One implementation, because two explanations of one pipeline would eventually
disagree and a diagnostic that names the wrong gate is worse than none.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from lumina.results.exclusions import active_rules
from lumina.survey.explain import explain
from lumina.survey.models import SurveySubmission


class Command(BaseCommand):
    help = "Explain why a survey submission is or is not in the published statistics."

    def add_arguments(self, parser):
        parser.add_argument(
            "identity", nargs="?", default=None,
            help="An identity hash (or its first characters), or a submission UUID. "
                 "Default: the most recent submissions.",
        )
        parser.add_argument(
            "--limit", type=int, default=3,
            help="How many recent submissions to explain when no identity is given.",
        )

    def handle(self, *args, **options):
        wanted = options["identity"]
        found = list(recent_submissions(wanted, options["limit"]))
        if not found:
            self.stdout.write(self.style.WARNING(
                "No submissions matched. The survey has none, or that identity is wrong."
            ))
            return

        rules = active_rules()
        self.stdout.write(f"{len(rules)} exclusion rule(s) active:")
        for rule in rules:
            self.stdout.write(
                f"    vendor={rule.vendor_id or '*'} device={rule.device_id or '*'} "
                f"kind={rule.kind or '*'} enabled={rule.enabled}  {rule.reason}"
            )
        if not rules:
            self.stdout.write("    (none)")

        for sub in found:
            self._print(sub, explain(sub, rules))

    def _print(self, sub, report) -> None:
        out = self.stdout
        ok, bad = self.style.SUCCESS, self.style.ERROR
        out.write("")
        out.write("=" * 72)
        out.write(f"submission {sub.uuid}  received {sub.received_at:%Y-%m-%d %H:%M} UTC")
        out.write(f"  identity   {sub.identity_hash or '(none)'} "
                  f"via {sub.identity_source or '(none)'}")
        out.write(f"  origin     {sub.origin} / {sub.trust_tier}")
        for check in report.checks:
            style = ok if check.ok else bad
            out.write(style(f"  {check.label:<18} {check.detail}"))
        for dev in report.devices:
            style = ok if dev.counted else bad
            suffix = "" if dev.counted else f" - {dev.reason}"
            verb = "counted" if dev.counted else "skipped"
            out.write(style(f"      {verb}  {dev.label} [{dev.ids}]{suffix}"))
        out.write(f"  cpu counted as     {report.cpu or '(none)'}")
        out.write(f"  period             {report.period}, "
                  f"{report.published_rows} published row(s)")
        out.write(f"  gpu_vendor rows    {report.gpu_buckets or '(none)'}")
        out.write((ok if report.verdict_ok else bad)(f"  RESULT             {report.verdict}"))


def recent_submissions(identity: str | None, limit: int):
    """The submissions to explain: one machine, or the most recent few.

    Shared with the reviewer's page only in spirit - the page always has its own
    submission - but kept beside the command so the matching rule for a partial identity
    is written once.
    """
    subs = SurveySubmission.objects.order_by("-received_at")
    if not identity:
        return subs[:limit]
    if len(identity) < 4:
        return subs.none()
    matched = subs.filter(identity_hash__startswith=identity)
    if matched.exists():
        return matched
    return subs.filter(uuid__startswith=identity) if _looks_like_uuid(identity) else subs.none()


def _looks_like_uuid(value: str) -> bool:
    return all(char in "0123456789abcdef-" for char in value.lower())
