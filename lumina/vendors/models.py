"""Vendors and per-user memberships.

A ``Vendor`` represents a hardware manufacturer, a software publisher, or an
AlmaLinux-internal org - ``Vendor.scope`` says which catalogs it appears in.
A ``VendorMembership`` binds a user to a vendor with a role that controls
whether they may submit listings on behalf of the vendor:

- ``member`` - read-only association (e.g. a vendor employee who hasn't been
  granted submission rights yet).
- ``submitter`` - may submit on behalf of the vendor.
- ``owner`` - may submit and manage the vendor's memberships (enforced at
  the view layer, not at the model layer).

``Vendor.verified`` is the admin-controlled flag that, combined with a
submit-role membership, lets a submission claim ``vendor`` trust level.

``VendorClaim`` is how a vendor takes over records a community member created on
their behalf: approving one grants owner membership and transfers ownership of
every listing already attributed to that vendor.
"""
from __future__ import annotations

from pathlib import Path
from typing import override

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from lumina.core.review import ReviewWorkflow


def _vendor_logo_path(instance: Vendor, filename: str) -> str:
    return str(Path("vendor-logos") / instance.slug / filename)


def _vendor_claim_evidence_path(instance: VendorClaim, filename: str) -> str:
    # Namespaced per claim so one requester's evidence is never served from
    # another's URL.
    return str(Path("vendor-claims") / str(instance.vendor_id or "new") / filename)


def _vendor_proposal_logo_path(instance: VendorProposal, filename: str) -> str:
    # Namespace by proposal id so each pending proposal's logo is isolated
    # from the live vendor's logo until approval copies it across.
    return str(Path("vendor-logos/proposals") / str(instance.pk or "new") / filename)


class VendorQuerySet(models.QuerySet["Vendor"]):
    def published(self) -> VendorQuerySet:
        return self.filter(published=True)

    def for_scope(self, scope: str) -> VendorQuerySet:
        """Vendors belonging to one catalog, including the ones in both.

        Hardware and software vendors are largely disjoint populations - a
        motherboard manufacturer and a backup-software publisher have no reason
        to appear in each other's pickers - but a handful of companies sell
        both, so ``both`` is a real third state rather than a modelling
        convenience.
        """
        return self.filter(scope__in=(scope, Vendor.SCOPE_BOTH))


class Vendor(models.Model):
    SCOPE_HARDWARE = "hardware"
    SCOPE_SOFTWARE = "software"
    SCOPE_BOTH = "both"
    SCOPE_CHOICES = [
        (SCOPE_HARDWARE, "Hardware"),
        (SCOPE_SOFTWARE, "Software"),
        (SCOPE_BOTH, "Hardware and software"),
    ]

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    # Defaults to hardware because every vendor that existed when this field was
    # added was a hardware vendor, which makes AddField's own backfill correct
    # and a data migration unnecessary.
    scope = models.CharField(
        max_length=16, choices=SCOPE_CHOICES, default=SCOPE_HARDWARE, db_index=True,
        help_text=(
            "Which catalog this vendor appears in. Memberships, aliases, and "
            "verification are shared regardless."
        ),
    )
    homepage = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True)
    logo = models.ImageField(upload_to=_vendor_logo_path, blank=True, null=True)
    verified = models.BooleanField(
        default=False,
        help_text=(
            "When true, submissions on behalf of this vendor are eligible for "
            "vendor-validated trust level. Toggled by AlmaLinux admins after "
            "verifying the vendor relationship."
        ),
    )
    published = models.BooleanField(
        default=True,
        help_text=(
            "False for vendors created inline on a hardware submission still "
            "awaiting reviewer approval. The public catalog only shows "
            "published vendors; admin sees both."
        ),
    )
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = VendorQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name

    @override
    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:140]
        super().save(*args, **kwargs)

    @property
    def is_claimed(self) -> bool:
        """Whether anyone has proven they represent this vendor.

        Derived from the existence of an owner membership rather than stored as a
        flag, so there is no second source of truth to drift. Submit rights are
        deliberately not enough: a community member who typed this vendor's name
        into a submit form holds ROLE_SUBMITTER, and the identity must stay
        available for the real vendor to claim.

        List pages should annotate with ``Exists`` instead of reading this per
        row.
        """
        return self.memberships.filter(role=VendorMembership.ROLE_OWNER).exists()


class VendorMembership(models.Model):
    ROLE_MEMBER = "member"
    ROLE_SUBMITTER = "submitter"
    ROLE_OWNER = "owner"
    ROLE_CHOICES = [
        (ROLE_MEMBER, "Member"),
        (ROLE_SUBMITTER, "Submitter"),
        (ROLE_OWNER, "Owner"),
    ]
    SUBMIT_ROLES = frozenset({ROLE_SUBMITTER, ROLE_OWNER})

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vendor_memberships",
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "vendor")]
        ordering = ["vendor__name"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user} @ {self.vendor} ({self.role})"

    @property
    def can_submit(self) -> bool:
        return self.role in self.SUBMIT_ROLES


class VendorProposal(ReviewWorkflow, models.Model):
    """Pending proposal for a new Vendor or an edit to an existing one.

    Two flavors share this table:

    - ``create``: ``target`` is null. On approval, a new Vendor is created
      from the proposed fields (and ``target`` is set to point at it for
      audit purposes).
    - ``update``: ``target`` points at an existing Vendor. On approval, the
      target vendor's fields are updated. Fields left blank on the proposal
      leave the vendor's current values alone. ``logo`` is only replaced if
      the proposal actually carries a new image.
    """

    review_noun = "proposal"

    KIND_CREATE = "create"
    KIND_UPDATE = "update"
    KIND_CHOICES = [(KIND_CREATE, "Create"), (KIND_UPDATE, "Update")]


    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    target = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, null=True, blank=True,
        related_name="proposals",
    )
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="vendor_proposals",
    )

    # Proposed field values. Names match Vendor so _apply can loop over them.
    name = models.CharField(max_length=120, blank=True)
    homepage = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True)
    description = models.TextField(blank=True)
    # Carried onto the Vendor this proposal creates. Without it a software vendor
    # proposed through the public form is created at the Vendor default
    # (hardware) and never appears in a software picker.
    scope = models.CharField(
        max_length=16, choices=Vendor.SCOPE_CHOICES, default=Vendor.SCOPE_HARDWARE,
    )
    logo = models.ImageField(upload_to=_vendor_proposal_logo_path, blank=True, null=True)

    status = models.CharField(
        max_length=16, choices=ReviewWorkflow.STATUS_CHOICES, default=ReviewWorkflow.STATUS_PENDING,
    )
    reviewer_notes = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="reviewed_vendor_proposals",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        # We previously enforced "create implies target is null" via a CHECK
        # constraint, but that made approval impossible - approving a create
        # proposal *should* set target to the newly-created Vendor so the
        # audit trail is queryable. The business rules are enforced in the
        # service layer (propose_new_vendor / propose_vendor_edit).

    def __str__(self) -> str:  # pragma: no cover - trivial
        subject = self.target.name if self.target else self.name
        return f"{self.get_kind_display()} vendor proposal: {subject}"

    # --- state transitions ------------------------------------------------
    def approve(self, *, by) -> None:
        self._require_open("approve")
        if self.kind == self.KIND_CREATE:
            vendor = Vendor.objects.create(
                name=self.name,
                homepage=self.homepage,
                contact_email=self.contact_email,
                description=self.description,
                scope=self.scope,
            )
            if self.logo:
                vendor.logo = self.logo
                vendor.save(update_fields=["logo"])
            self.target = vendor
        else:
            self._apply_update()
        self.status = self.STATUS_APPROVED
        self.reviewed_by = by
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "target"])



    # --- internals --------------------------------------------------------
    # Fields copied from proposal → vendor on update-approval. Blank string
    # values are treated as "no change": we don't want an empty homepage on
    # the proposal to blank out a populated homepage on the vendor.
    _COPIED_FIELDS = ("name", "homepage", "contact_email", "description")

    def _apply_update(self) -> None:
        assert self.target is not None
        changed: list[str] = []
        for field in self._COPIED_FIELDS:
            new = getattr(self, field)
            if new and getattr(self.target, field) != new:
                setattr(self.target, field, new)
                changed.append(field)
        if self.logo:
            self.target.logo = self.logo
            changed.append("logo")
        if changed:
            self.target.save(update_fields=changed)


class VendorAlias(models.Model):
    """Alternate spelling of a vendor's name as it appears in DMI data.

    Hardware reports the manufacturer however the firmware author typed it:
    "Dell" vs "Dell Inc.", "ASUSTeK COMPUTER INC." vs "ASUS". Aliases pin
    the mappings that normalization alone cannot derive (e.g. "MSI" for
    "Micro-Star International"); ``vendors.services.resolve_vendor`` tries
    exact names, then aliases, then normalized comparison.
    """

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField(max_length=120, unique=True)

    class Meta:
        verbose_name_plural = "vendor aliases"

    def __str__(self) -> str:
        return f"{self.name} -> {self.vendor.name}"


# Defined at module level because the nested ``Meta`` class body cannot see its
# enclosing class's attributes, and the constraint below needs the same tuple the
# state machine uses. One definition, two scopes.
_OPEN_CLAIM_STATUSES = ("pending", "needs-changes")


class VendorClaim(ReviewWorkflow, models.Model):
    """A request to be recognised as representing a vendor.

    The catalog fills up with vendors that nobody from those companies created:
    a community member submits "Acme Backup", types "Acme" as the publisher, and
    a vendor record exists with its identity unassigned. This is how the real
    Acme takes it over.

    The claim targets the **vendor**, not a listing, because that is what is
    actually being claimed - and because listing ownership then follows for
    everything already attributed to them, across both catalogs, in one reviewer
    action rather than one per listing.

    Evidence is a stated case judged by a reviewer rather than an automated
    domain proof: the SIG can always ask for more out of band, and a DNS or
    well-known-file check fails for exactly the engineer who cannot edit their
    own company's DNS.
    """

    review_noun = "claim"

    OPEN_STATUSES = _OPEN_CLAIM_STATUSES

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="claims")
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="vendor_claims",
    )
    work_email = models.EmailField(
        help_text="An address at the vendor's own domain carries the most weight."
    )
    role_at_vendor = models.CharField(max_length=120)
    note = models.TextField(blank=True)
    evidence = models.FileField(upload_to=_vendor_claim_evidence_path, blank=True)

    status = models.CharField(
        max_length=16, choices=ReviewWorkflow.STATUS_CHOICES, default=ReviewWorkflow.STATUS_PENDING
    )
    reviewer_notes = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="reviewed_vendor_claims",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            # Belt only. Django's MariaDB backend reports
            # supports_partial_indexes = False, so a conditional constraint is
            # skipped at migration time with system check models.W036 and exists
            # only on SQLite. `services.claim_vendor` is what actually enforces
            # this in production.
            models.UniqueConstraint(
                fields=["vendor", "requester"],
                condition=models.Q(status__in=_OPEN_CLAIM_STATUSES),
                name="one_open_claim_per_vendor_and_requester",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.requester} claims {self.vendor}"

    def approve(self, *, by, verify: bool, demote_others: bool = True) -> dict[str, int]:
        """Hand the vendor's identity to the requester.

        Returns the per-model counts of listings whose ownership transferred, so
        the reviewer can be told what the approval actually did.

        ``verify`` is separate on purpose: a claim establishes *who* someone
        represents, while ``Vendor.verified`` is the trust decision that lets
        them self-certify at the vendor tier. One reviewer action can do both,
        but conflating the fields would mean every claim approval silently
        granted self-certification.

        ``demote_others`` drops any existing submit-role member to plain member,
        so verifying the vendor does not hand the vendor tier
        (``derive_allowed_levels`` grants it to any submit-role member of a
        *verified* vendor) to whoever was on the roster before this claim.
        Inline creation no longer enrolls the person who named the vendor, so a
        freshly proposed vendor has no such member - but this still guards one
        added by any other route. Off only when the reviewer can see the
        existing members are colleagues.
        """
        from lumina.vendors.services import transfer_unowned_listings

        self._require_open("approve")

        VendorMembership.objects.update_or_create(
            user=self.requester, vendor=self.vendor,
            defaults={"role": VendorMembership.ROLE_OWNER},
        )
        if demote_others:
            VendorMembership.objects.filter(
                vendor=self.vendor, role__in=VendorMembership.SUBMIT_ROLES,
            ).exclude(user=self.requester).update(role=VendorMembership.ROLE_MEMBER)

        vendor_changed = []
        if not self.vendor.published:
            self.vendor.published = True
            vendor_changed.append("published")
        if verify and not self.vendor.verified:
            self.vendor.verified = True
            vendor_changed.append("verified")
        if vendor_changed:
            self.vendor.save(update_fields=vendor_changed)

        self._record_approval(by=by)

        return transfer_unowned_listings(self.vendor, by=by)


