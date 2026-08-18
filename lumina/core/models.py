"""Model mixins shared by the two catalogs.

Plain mixins, not Django abstract models. Nothing here declares a field, so nothing
here can emit a migration - which matters because the alternative was tempting and
wrong: hoisting ``status``/``reviewed_by`` into an abstract base would have rewritten
every ``related_name`` and broken the reverse accessors that views and templates use
by name.
"""
from __future__ import annotations

from django.utils.text import slugify


class VendorSlugMixin:
    """A unique, vendor-prefixed slug, generated on first save.

    Shared by ``HardwareListing`` and ``Software``, whose implementations were
    identical: the same vendor prefix, the same 200-character truncation against a
    220-character column, and the same ``-2``/``-3`` uniquifying loop.

    The vendor prefix is what makes two vendors' identically named products
    distinguishable - "PowerEdge R760" from Dell and from a reseller cannot share
    ``poweredge-r760``. A listing with no vendor yet (an inline proposal mid-creation)
    falls back to the bare name.

    ``type(self)`` rather than a hard-coded manager, so a subclass slugs against its
    own table. The two implementations differed only there, and only cosmetically:
    software hard-coded ``Software.objects``, which is the same manager
    ``type(self)`` resolves to.

    ``exclude(pk=self.pk)`` so re-saving an existing row does not collide with
    itself, which matters because ``save()`` is also the update path.

    Not used by ``Vendor`` or ``CategoryValue``: those slug at 140 characters with no
    vendor prefix and no uniquifying loop, which is a different rule rather than a
    variation on this one.
    """

    SLUG_BASE_MAX_LENGTH = 200

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self.generate_unique_slug()
        super().save(*args, **kwargs)

    def generate_unique_slug(self) -> str:
        limit = self.SLUG_BASE_MAX_LENGTH
        if self.vendor_id:
            base = slugify(f"{self.vendor.slug}-{self.name}")[:limit]
        else:
            base = slugify(self.name)[:limit]
        candidate, suffix = base, 2
        model = type(self)
        while model.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate
