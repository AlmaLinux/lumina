"""Model mixins shared by the two catalogs, and the site-wide display settings.

The mixins are plain mixins, not Django abstract models, and declare no fields - which
matters because the alternative was tempting and wrong: hoisting
``status``/``reviewed_by`` into an abstract base would have rewritten every
``related_name`` and broken the reverse accessors that views and templates use by name.

``DisplayDefaults`` is the exception and a real model: a setting that governs pages in
several apps belongs to none of them.
"""
from __future__ import annotations

from typing import override

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
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


class DisplayDefaults(models.Model):
    """How much of a long list a page shows at once. One row, edited in the admin.

    A constant in code meant that the number nobody had thought hard about was the number
    everybody lived with, and changing it was a deploy. The dashboard in particular had no
    pagination at all: its lists were cut off at two hundred rows silently, and the filter boxes
    searched only the rows that had survived the cut, so a submitter looking for their own run
    from last year was told it did not exist.
    """

    rows_per_page = models.PositiveIntegerField(
        default=20,
        validators=[MinValueValidator(1), MaxValueValidator(500)],
        help_text="Rows per page on the dashboard and the review archive. "
                  "Between 1 and 500.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "display defaults"
        verbose_name_plural = "display defaults"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "display defaults"

    @override
    def save(self, *args, **kwargs):
        # One row, whatever anybody does with it.
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> DisplayDefaults:
        """The one row, created with the built-in default the first time it is asked for."""
        return cls.objects.get_or_create(pk=1)[0]

    @classmethod
    def per_page(cls) -> int:
        """Rows per page, never zero.

        ``Paginator`` raises on a per-page of nothing, and a row that has been edited to zero -
        or written before the validators existed - would take down every page that reads this.
        """
        return max(1, cls.load().rows_per_page or 1)
