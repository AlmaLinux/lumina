"""Merge one vendor into another: manage.py merge_vendors <survivor> <duplicate>.

Both arguments accept a vendor name or slug. The duplicate's references all
move to the survivor and its name becomes an alias, so the same DMI string
can never re-create the split.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from lumina.vendors.models import Vendor
from lumina.vendors.services import merge_vendors


class Command(BaseCommand):
    help = "Merge a duplicate vendor into a surviving one, leaving an alias behind."

    def add_arguments(self, parser):
        parser.add_argument("survivor", help="name or slug of the vendor to keep")
        parser.add_argument("duplicate", help="name or slug of the vendor to fold in")

    def _resolve(self, ref: str) -> Vendor:
        vendor = Vendor.objects.filter(Q(name__iexact=ref) | Q(slug=ref)).first()
        if vendor is None:
            raise CommandError(f"No vendor named or slugged {ref!r}.")
        return vendor

    def handle(self, *args, **options):
        survivor = self._resolve(options["survivor"])
        duplicate = self._resolve(options["duplicate"])
        if survivor == duplicate:
            raise CommandError("Survivor and duplicate are the same vendor.")
        moved = merge_vendors(survivor, duplicate)
        summary = ", ".join(f"{v} {k}" for k, v in moved.items() if v)
        self.stdout.write(self.style.SUCCESS(
            f"Merged {options['duplicate']!r} into {survivor.name!r}"
            + (f" ({summary})" if summary else "")
        ))
        self.stdout.write(
            f"“{duplicate.name}” is now an alias of “{survivor.name}”."
        )
