"""RSS/Atom feeds of newly published results.

Part of the gamification story: the community can follow new certifications
and benchmark numbers without polling the site. Only ``public()`` runs are
listed, so embargoed submissions never leak through a feed.
"""
from __future__ import annotations

from django.contrib.syndication.views import Feed
from django.urls import reverse
from django.utils.feedgenerator import Atom1Feed

from lumina.results.models import RunType, TestRun


class ValidationFeed(Feed):
    feed_type = Atom1Feed
    # "systems" was accurate while every run was a machine. The feed now carries component claims
    # too, and a syndicated, archived assertion that a machine was validated is the hardest kind to
    # take back.
    title = "AlmaLinux certification - latest validated hardware"
    description = "Hardware validation runs recently published on Lumina."

    def link(self):
        return reverse("results:latest_validations")

    def items(self):
        return (
            TestRun.objects.public()
            .filter(run_type=RunType.validate.value)
            .select_related("alma_release", "listing_system")
            .order_by("-published_at")[:25]
        )

    def item_title(self, item: TestRun) -> str:
        release = f" on AlmaLinux {item.alma_release.major}" if item.alma_release else ""
        verdict = "validated" if item.verdict() else "tested"
        # ``display_name`` already names the component rather than the host on a scoped run; the
        # scope goes in as well, because an entry title is often all a reader ever sees of a feed.
        scope = f" ({', '.join(item.scope_labels)} only)" if item.is_scoped else ""
        return f"{item.display_name} {verdict}{release}{scope}"

    def item_description(self, item: TestRun) -> str:
        counts = item.status_counts()
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        # The host CPU was the only hardware named here, unlabelled, which on a scoped entry
        # described a part the run says nothing about.
        if item.is_scoped:
            subject = item.claim_subject or ", ".join(item.scope_labels)
            return f"{subject} in {item.host_name} - {summary}"
        return f"{item.cpu_model or 'Unknown CPU'} - {summary}"

    def item_link(self, item: TestRun) -> str:
        return item.get_absolute_url()

    def item_pubdate(self, item: TestRun):
        return item.published_at


class BenchmarkFeed(Feed):
    feed_type = Atom1Feed
    title = "AlmaLinux certification - latest benchmark results"
    description = "Benchmark runs recently published on Lumina."

    def link(self):
        return reverse("benchmarks:index")

    def items(self):
        return (
            TestRun.objects.public()
            .with_benchmarks()
            .prefetch_related("benchmarks")
            .order_by("-published_at")[:25]
        )

    def item_title(self, item: TestRun) -> str:
        return f"Benchmarks: {item.display_name}"

    def item_description(self, item: TestRun) -> str:
        highlights = []
        for row in item.benchmarks.filter(is_primary=True)[:5]:
            highlights.append(f"{row.benchmark_id}: {row.value:g} {row.unit}")
        return "; ".join(highlights) or item.cpu_model

    def item_link(self, item: TestRun) -> str:
        return item.get_absolute_url()

    def item_pubdate(self, item: TestRun):
        return item.published_at
