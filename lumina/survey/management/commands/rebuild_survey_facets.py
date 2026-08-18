"""Rewrite the derived facet columns of every survey submission from its stored payload.

The columns are an index for segments and admin search, not a source of truth: they hold what a
naming rule said on the day a machine reported. When a rule is corrected - an AMD APU named from
its CPU brand string rather than from lspci's die codename, say - the published statistics fix
themselves on the next rollup, because the rollup derives, but the columns keep the old answer and
a segment filtering on one would select the wrong machines. This brings them back into step.

The payload is never touched. There is a reviewer button for this beside the statistics rebuild,
which is where it is actually reached from; this exists for a deploy hook and for a census large
enough that somebody would rather not hold a request open.
"""
from __future__ import annotations

from typing import override

from django.core.management.base import BaseCommand

from lumina.survey import facets


class Command(BaseCommand):
    help = "Rewrite survey facet columns from each submission's stored payload."

    @override
    def handle(self, *args, **options):
        changed = facets.rebuild()
        self.stdout.write(self.style.SUCCESS(
            f"Facet columns rewritten for {changed} submission(s)."
        ))
