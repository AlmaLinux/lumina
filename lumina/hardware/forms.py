"""Submission form.

One flow: create a draft System/Component plus a pending Submission. This is the
*declared* route into the catalog, for hardware that cannot produce a passing
AlmaLinux run yet (pre-release or embargoed silicon, a borrowed machine, a vendor's
paper evidence). Everything it produces is declared, never proven, and it is capped
at the community tier in ``Submission.approve``. Certification comes from the suite.

It used to have a second flow, and that flow was the most dangerous code in the
project. Posting a ``listing_slug`` made the form reuse an *existing* listing, and
because ``_attach_release_versions`` ran from ``save()`` rather than from
``approve()``, the write landed before any review. There was no ownership check and
no ``published`` filter, so any logged-in account could name any listing and
``update_or_create`` its ``ListingVersion``. Driven end to end, a brand-new user with
no vendor membership rewrote a Dell-owned, run-proven "AlmaLinux 9.6+" row down to
"9.1" while their submission still sat pending. The row kept ``source='run'``, so the
public page and the API attributed the downgrade to a validation run that had never
happened, rejecting the submission did not revert it, and a later genuine 9.6 run
could not repair it because ``record_compatibility`` only ever lowered a floor toward
proven ground. It was the only writer in the codebase able to move ``minimum_minor``
against evidence. (That field is gone - hardware certifies per major now - so the
downgrade it describes is no longer expressible. The lesson about who may write a
proven row stands, and now applies to ``source``.) Deleting it also removed the phantom per-release attestations and a
500 on a mistyped slug.

Re-validating a listing means running the suite against it. That path already exists,
is manifest-verified, and is the one that earns a tier.

UX decisions pinned down here (so the template stays dumb):

- Vendor is a ``<select>`` of existing Vendors, plus a "Propose a new vendor"
  option that creates one unpublished for a reviewer (``INLINE_VENDOR_SENTINEL``,
  ``_resolve_inline_vendor``). The fragmentation risk that once argued for
  prohibiting this - a mis-spelled "dell" beside "Dell EMC" - is handled by the
  duplicate-name check in ``clean`` instead, which points the submitter at the
  existing vendor rather than minting a second one.
- Each Category the submitter may tag the listing with is rendered as a
  multi-select of approved values PLUS a free-text "propose new" field.
  The fields are named ``cat_<category-slug>`` and ``propose_<category-slug>``
  so the form class can discover them dynamically.
- For System submissions, a ``cpus`` multi-select lists all CPU-kind
  Components. Selecting one attaches that CPU to ``system.cpus``.
"""
from __future__ import annotations

from django import forms
from django.db import transaction

from lumina.core.certification import ValidationLevel
from lumina.core.files import hash_upload, validate_evidence_file
from lumina.core.forms import bootstrapify, narrow_level_field
from lumina.hardware.models import (
    Component,
    ComponentKind,
    HardwareListing,
    ListingCategoryValue,
    ListingEditProposal,
    ListingVersion,
    Submission,
    System,
    TestResultAttachment,
    listing_fk,
)
from lumina.hardware.services import attach_cpu
from lumina.releases.models import AlmaLinuxRelease
from lumina.taxonomy.forms import category_picker_field
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor, VendorMembership
from lumina.vendors.services import create_inline_vendor, resolve_claimed_level

CATEGORY_FIELD_PREFIX = "cat_"
PROPOSE_FIELD_PREFIX = "propose_"
RELEASE_SUPPORT_PREFIX = "release_support_"
NEW_CPU_NAME_PREFIX = "new_cpu_name_"
NEW_CPU_VENDOR_PREFIX = "new_cpu_vendor_"
NEW_CPU_DESCRIPTION_PREFIX = "new_cpu_description_"
INLINE_VENDOR_SENTINEL = "__new__"
NEW_CPU_ROW_COUNT = 3  # rendered inline-cpu rows in the submit form


class ReviewerListingEditForm(forms.Form):
    """Lets a reviewer tweak a pending submission's listing - and any
    inline-proposed vendor and CPUs attached to it - directly from the
    review detail page, before approving the submission. Changes are
    applied immediately to the live rows; approval is a separate step.

    Fields are dynamic because the inline vendor and inline CPU sections
    only appear when the submitter actually used those flows.
    """

    name = forms.CharField(max_length=200)
    model_number = forms.CharField(max_length=120, required=False)
    vendor_spec_url = forms.URLField(required=False, assume_scheme="https")
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    def __init__(self, *args, listing: HardwareListing, **kwargs):
        self.listing = listing
        kwargs.setdefault("initial", {}).update(
            name=listing.name,
            model_number=listing.model_number,
            vendor_spec_url=listing.vendor_spec_url,
            description=listing.description,
        )
        super().__init__(*args, **kwargs)

        self._inline_vendor: Vendor | None = None
        if not listing.vendor.published:
            self._inline_vendor = listing.vendor
            self.fields["vendor_name"] = forms.CharField(
                max_length=120, initial=listing.vendor.name,
                label="Vendor name (inline)",
            )
            self.fields["vendor_homepage"] = forms.URLField(
                required=False, assume_scheme="https",
                initial=listing.vendor.homepage,
            )

        self._inline_cpu_fields: list[tuple[Component, str, str]] = []
        if isinstance(listing, System):
            cpu_vendor_choices = [
                (v.slug, v.name) for v in Vendor.objects.published().order_by("name")
            ]
            for cpu in listing.cpus.filter(published=False):
                name_f = f"cpu_{cpu.pk}_name"
                vendor_f = f"cpu_{cpu.pk}_vendor"
                self.fields[name_f] = forms.CharField(
                    max_length=200, initial=cpu.name, label=f"CPU #{cpu.pk} name",
                )
                self.fields[vendor_f] = forms.ChoiceField(
                    choices=cpu_vendor_choices, initial=cpu.vendor.slug,
                    label=f"CPU #{cpu.pk} vendor",
                )
                self._inline_cpu_fields.append((cpu, name_f, vendor_f))

        bootstrapify(self)

    @property
    def inline_cpu_groups(self) -> list[dict]:
        return [
            {"cpu": cpu, "name_bound_field": self[name_f], "vendor_bound_field": self[vendor_f]}
            for cpu, name_f, vendor_f in self._inline_cpu_fields
        ]

    @property
    def has_inline_vendor(self) -> bool:
        return self._inline_vendor is not None

    def save(self) -> None:
        listing = self.listing
        listing.name = self.cleaned_data["name"]
        listing.model_number = self.cleaned_data["model_number"]
        listing.vendor_spec_url = self.cleaned_data["vendor_spec_url"]
        listing.description = self.cleaned_data["description"]
        listing.save(
            update_fields=["name", "model_number", "vendor_spec_url", "description"]
        )
        if self._inline_vendor is not None:
            v = self._inline_vendor
            v.name = self.cleaned_data["vendor_name"]
            v.homepage = self.cleaned_data.get("vendor_homepage") or ""
            v.save(update_fields=["name", "homepage"])
        for cpu, name_f, vendor_f in self._inline_cpu_fields:
            cpu.name = self.cleaned_data[name_f]
            new_vendor = Vendor.objects.filter(slug=self.cleaned_data[vendor_f]).first()
            if new_vendor:
                cpu.vendor = new_vendor
            cpu.save(update_fields=["name", "vendor"])


class ListingEditProposalForm(forms.ModelForm):
    """Pre-fills with the listing's current values; submitter overrides
    only what they want to change. Blank fields stay blank in the saved
    proposal and are treated as 'no change' at approval time."""

    class Meta:
        model = ListingEditProposal
        fields = ("name", "model_number", "description", "vendor_spec_url", "submitter_notes")
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "submitter_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, listing: HardwareListing, **kwargs):
        kwargs.setdefault("initial", {}).update(
            name=listing.name,
            model_number=listing.model_number,
            description=listing.description,
            vendor_spec_url=listing.vendor_spec_url,
        )
        super().__init__(*args, **kwargs)
        bootstrapify(self)


class SubmissionForm(forms.Form):
    KIND_CHOICES = [("system", "System"), ("component", "Component")]

    kind = forms.ChoiceField(choices=KIND_CHOICES)

    name = forms.CharField(max_length=200, required=False)
    model_number = forms.CharField(max_length=120, required=False)
    vendor_spec_url = forms.URLField(
        required=False,
        assume_scheme="https",
        widget=forms.URLInput(attrs={"placeholder": "https://vendor.example/specs/…"}),
    )
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    # Vendor pickers are populated at __init__ time from current DB state so
    # newly-added Vendors are picked up without a server restart.
    # vendor accepts the special INLINE_VENDOR_SENTINEL ("__new__") to mean
    # "create a new vendor inline using the new_vendor_* fields below."
    vendor = forms.ChoiceField(
        choices=[], required=False, widget=forms.Select(attrs={
            # A searchable list, not a native dropdown: the publisher list is
            # unbounded and will run to thousands, where a <select> means scrolling
            # and the browser's prefix-only type-ahead. combobox.js replaces the
            # picking; the select still holds the value, so server-side validation
            # and no-JS use are untouched.
            "data-combobox": "true",
            "data-placeholder": "Search vendors\u2026",
            # The menu caps at twelve entries, and "+ Add a new vendor" is last in
            # the option list, so without pinning it falls off the bottom the moment
            # there are more than twelve vendors and the inline flow becomes
            # undiscoverable.
            "data-combobox-pin": INLINE_VENDOR_SENTINEL,
            "data-combobox-pin-target": "#id_new_vendor_name",
        }),
    )
    on_behalf_of = forms.ChoiceField(choices=[], required=False)

    # Inline new-vendor sidecar fields, only used when vendor == "__new__".
    new_vendor_name = forms.CharField(max_length=120, required=False)
    new_vendor_homepage = forms.URLField(required=False, assume_scheme="https")
    new_vendor_contact_email = forms.EmailField(required=False)
    new_vendor_description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}), required=False,
    )
    new_vendor_logo = forms.ImageField(required=False)

    claimed_validation_level = forms.ChoiceField(choices=ValidationLevel.choices)
    submitter_notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)

    # No ``validators=`` here: a FileField cleans one value and ``MultiValueDict`` gives
    # it the *last* posted file, so a field validator checks whichever file happened to
    # be last and misses the rest. ``clean_attachments`` walks every one of them, which
    # makes it a superset - proven by deleting this line and watching the suite stay
    # green. Two overlapping gates on one field is how they drift apart.
    attachments = forms.FileField(required=False)

    # Set by the submitter only after they have been shown the possible duplicates. See
    # ``_warn_about_duplicates``. Rendered by the template only when the error is
    # present, so an ordinary submission never sees a box asking about a problem it does
    # not have.
    confirm_not_duplicate = forms.BooleanField(required=False)

    # System-only picker: CPU components. Populated in __init__.
    cpus = forms.MultipleChoiceField(choices=[], required=False)

    def __init__(self, *args, user, submission: Submission | None = None, **kwargs):
        self.user = user
        # Revise mode: edit the draft this submission already created and put the same
        # row back in the queue, rather than creating a second listing and a second
        # submission. Mirrors ``SoftwareSubmissionForm``, which had this from the start.
        self.submission = submission
        # Unconditional, as in SoftwareSubmissionForm: Django ignores ``initial`` for a
        # bound form, so posted data still wins. Guarding this on "is there data" is
        # where the first attempt went wrong - the view passes ``request.POST or None``
        # positionally, so on a GET ``args`` is ``(None, None)``, which is truthy, and
        # the prefill silently never ran.
        if submission is not None:
            kwargs.setdefault("initial", {}).update(self._initial_from(submission))
        super().__init__(*args, **kwargs)
        narrow_level_field(self, self.user)

        vendor_choices = (
            [("", "- Select vendor -")]
            + [(v.slug, v.name) for v in Vendor.objects.published().order_by("name")]
            + [(INLINE_VENDOR_SENTINEL, "+ Add a new vendor…")]
        )
        self.fields["vendor"].choices = vendor_choices

        on_behalf_choices = [("", "- None (submitting as user) -")] + [
            (m.vendor.slug, m.vendor.name)
            for m in VendorMembership.objects.filter(
                user=user, role__in=VendorMembership.SUBMIT_ROLES
            ).select_related("vendor")
        ]
        self.fields["on_behalf_of"].choices = on_behalf_choices

        self.fields["cpus"].choices = [
            (str(c.pk), f"{c.vendor.name} - {c.name}")
            for c in Component.of_kind(ComponentKind.cpu).select_related("vendor").order_by("vendor__name", "name")
        ]

        # Inline new-CPU rows. We render NEW_CPU_ROW_COUNT empty rows by
        # default; future iteration can grow this dynamically with HTMX or JS.
        # Each row is (name, vendor) - vendor must already exist (CPU vendors
        # like Intel/AMD are typically a different vendor than the system OEM).
        cpu_vendor_choices = [("", "- Select CPU vendor -")] + [
            (v.slug, v.name) for v in Vendor.objects.published().order_by("name")
        ]
        self._new_cpu_rows: list[tuple[str, str, str]] = []
        for i in range(NEW_CPU_ROW_COUNT):
            name_f = f"{NEW_CPU_NAME_PREFIX}{i}"
            vendor_f = f"{NEW_CPU_VENDOR_PREFIX}{i}"
            desc_f = f"{NEW_CPU_DESCRIPTION_PREFIX}{i}"
            self.fields[name_f] = forms.CharField(max_length=200, required=False)
            self.fields[vendor_f] = forms.ChoiceField(
                choices=cpu_vendor_choices, required=False,
            )
            self.fields[desc_f] = forms.CharField(
                required=False, widget=forms.TextInput(),
            )
            self._new_cpu_rows.append((name_f, vendor_f, desc_f))

        # One checkbox per known AlmaLinuxRelease. Off means "not certified for this major".
        #
        # A minimum-minor box sat beside each one. Certification is per major now, the way the
        # software catalog always has been, so there is nothing for it to hold.
        self._release_fields: list[tuple[AlmaLinuxRelease, str]] = []
        for release in AlmaLinuxRelease.objects.supported():
            support_f = f"{RELEASE_SUPPORT_PREFIX}{release.major}"
            self.fields[support_f] = forms.BooleanField(
                required=False, label=str(release),
            )
            self._release_fields.append((release, support_f))

        # Dynamic per-category fields. Each Category gets:
        #   cat_<slug>:      ChoiceField (dropdown) or MultipleChoiceField
        #                    (checkboxes / multiselect) per category.picker_widget.
        #   propose_<slug>:  CharField for a new-value proposal - only when
        #                    category.allow_suggestions is True.
        # Every hardware scope, which is every scope except software. One form
        # serves both system and component submissions and the kind is a posted
        # field rather than a constructor argument, so this cannot narrow further
        # the way ``results/forms.py`` does - but it must at least exclude the
        # software-only categories, or a server submission is offered Backup and
        # Creative to tag a machine with.
        hardware_scopes = [
            Category.APPLIES_SYSTEM,
            Category.APPLIES_COMPONENT,
            Category.APPLIES_BOTH,
        ]
        self._category_fields: list[tuple[Category, str, str | None]] = []
        for category in (
            Category.objects.filter(applies_to__in=hardware_scopes)
            # A derived facet is set from an approved run's own report, so asking
            # the submitter would invite an answer that contradicts the machine.
            .exclude(derived_from_runs=True)
            .prefetch_related("values")
            .order_by("display_order", "name")
        ):
            approved = [
                (v.slug, v.value)
                for v in category.values.all()
                if v.status == CategoryValue.STATUS_APPROVED
            ]
            cat_field = f"{CATEGORY_FIELD_PREFIX}{category.slug}"
            self.fields[cat_field] = self._build_category_field(category, approved)

            propose_field: str | None = None
            if category.allow_suggestions:
                propose_field = f"{PROPOSE_FIELD_PREFIX}{category.slug}"
                self.fields[propose_field] = forms.CharField(
                    required=False, label=f"Propose new {category.name.lower()}",
                    widget=forms.TextInput(attrs={"placeholder": f"e.g. new {category.name.lower()}"}),
                )
            self._category_fields.append((category, cat_field, propose_field))

        # Bootstrap-ify widgets so the template renders cleanly without
        # per-field boilerplate. This form used to carry its own copy of the
        # rule; the copy is what let the shared helper drift into missing
        # CheckboxSelectMultiple, so there is deliberately only one now.
        bootstrapify(self)

    @staticmethod
    def _build_category_field(category: Category, approved: list[tuple[str, str]]) -> forms.Field:
        """Delegates to ``taxonomy.forms``; the run-proposal form had the same three
        branches inline."""
        return category_picker_field(category, approved)

    # --- Template helpers ---------------------------------------------------
    @property
    def release_field_groups(self) -> list[dict]:
        """Paired (release, support bound field) so the template can render one row per
        AlmaLinux release without reaching into form internals."""
        return [
            {"release": release, "support_bound_field": self[support_f]}
            for release, support_f in self._release_fields
        ]

    @property
    def new_cpu_row_groups(self) -> list[dict]:
        """Bound fields for the inline new-CPU rows so the template can
        iterate them without reaching into form internals."""
        return [
            {
                "name_bound_field": self[name_f],
                "vendor_bound_field": self[vendor_f],
                "description_bound_field": self[desc_f],
            }
            for name_f, vendor_f, desc_f in self._new_cpu_rows
        ]

    @property
    def category_field_groups(self) -> list[dict]:
        """Paired (category, picker bound field, optional propose bound field).

        ``propose_bound_field`` is None when the category disallows suggestions
        - the template uses that to skip rendering the inline input.
        """
        return [
            {
                "category": category,
                "multiselect_bound_field": self[cat_field],
                "propose_bound_field": self[propose_field] if propose_field else None,
            }
            for category, cat_field, propose_field in self._category_fields
        ]

    # --- Validation ---------------------------------------------------------
    def clean_vendor(self) -> Vendor | str | None:
        """Returns either an existing Vendor, the inline-vendor sentinel, or None.

        We can't materialize the new Vendor here because clean_*() methods
        run before the full clean(), and we need the new_vendor_* fields to
        be cleaned first. The actual creation happens in ``save()``.
        """
        slug = self.cleaned_data.get("vendor")
        if not slug:
            return None
        if slug == INLINE_VENDOR_SENTINEL:
            return INLINE_VENDOR_SENTINEL
        try:
            return Vendor.objects.get(slug=slug)
        except Vendor.DoesNotExist as exc:
            raise forms.ValidationError("Unknown vendor.") from exc

    def clean(self):
        cleaned = super().clean()
        cleaned["claimed_validation_level"] = self._resolve_level(cleaned)
        if cleaned.get("vendor") == INLINE_VENDOR_SENTINEL:
            new_name = (cleaned.get("new_vendor_name") or "").strip()
            if not new_name:
                self.add_error("new_vendor_name", "Required when proposing a new vendor.")
            elif Vendor.objects.filter(name__iexact=new_name).exists():
                self.add_error(
                    "new_vendor_name",
                    "A vendor with this name already exists. Pick it from the dropdown instead.",
                )
        # Declared unconditionally now. These read ``required=False`` on the field and
        # are enforced here instead, because the removed re-validation flow supplied
        # them from the named listing and so had to skip the check.
        if not cleaned.get("name"):
            self.add_error("name", "Required.")
        if not cleaned.get("vendor"):
            self.add_error("vendor", "Required.")
        self._warn_about_duplicates(cleaned)
        return cleaned

    # A listing name that already exists, checked before the fork happens rather than
    # after. The reviewer-side banner catches these too, but a duplicate caught here
    # costs one checkbox and a duplicate caught there costs a round trip through the
    # queue - and a fork that nobody catches costs one machine listed twice, each copy
    # carrying half the evidence.
    CONFIRM_DUPLICATE_FIELD = "confirm_not_duplicate"

    def _warn_about_duplicates(self, cleaned: dict) -> None:
        """Refuse once, with the matches named, unless the submitter overrides.

        Not a hard block. The matcher works on hand-typed names and cannot know that a
        submitter has a genuinely different machine whose name normalizes the same way,
        so the last word has to be theirs. Refusing *once* is the difference between
        making them look and letting them barrel through: a warning they can ignore
        without acknowledging is a warning nobody reads.

        Skipped entirely on a revision, where the listing being edited already exists
        and would helpfully report itself as its own duplicate.
        """
        from lumina.hardware.services import similar_listings

        if self.submission is not None:
            return
        if self.errors or cleaned.get(self.CONFIRM_DUPLICATE_FIELD):
            return
        vendor = cleaned.get("vendor")
        name = cleaned.get("name")
        if not name or not vendor or vendor == INLINE_VENDOR_SENTINEL:
            # An inline vendor is new by definition, so nothing under it can collide.
            return

        model = System if cleaned.get("kind") == "system" else Component
        # Unsaved, purely so ``similar_listings`` can key off it. Never saved: this runs
        # during validation and the real listing is created in ``save()``.
        probe = model(
            vendor=vendor, name=name,
            model_number=cleaned.get("model_number") or "",
        )
        matches = similar_listings(probe, limit=3)
        if not matches:
            return

        listed = ", ".join(f"{other.vendor.name} {other.name}" for other, _ in matches)
        self.add_error(
            self.CONFIRM_DUPLICATE_FIELD,
            f"The catalog may already have this: {listed}. Adding evidence to an "
            "existing listing means running the certification suite against it, not "
            "filing a second listing. If yours really is different hardware, tick this "
            "box and submit again.",
        )

    def clean_attachments(self):
        """Validate *every* posted file, which is the only gate on this field.

        ``_attach_files`` iterates ``self.files.getlist("attachments")`` and stores all
        of them, so all of them have to be checked. A ``validators=`` entry on the field
        cannot do it: ``FileField`` cleans a single value and ``MultiValueDict`` hands it
        the last posted file, so a hostile first file walks straight past.
        """
        files = self.files.getlist("attachments") if self.files else []
        for upload in files:
            validate_evidence_file(upload)
        return self.cleaned_data.get("attachments")

    def clean_on_behalf_of(self) -> Vendor | None:
        slug = self.cleaned_data.get("on_behalf_of")
        if not slug:
            return None
        try:
            vendor = Vendor.objects.get(slug=slug)
        except Vendor.DoesNotExist as exc:
            raise forms.ValidationError("Unknown vendor.") from exc
        if not VendorMembership.objects.filter(
            user=self.user, vendor=vendor, role__in=VendorMembership.SUBMIT_ROLES
        ).exists():
            raise forms.ValidationError("You cannot submit on behalf of this vendor.")
        return vendor

    def _resolve_level(self, cleaned: dict) -> str:
        """The tier this submission carries, from who and for whom.

        Attribution decides it: submitting on behalf of a vendor **is** the vendor
        claim, so the posted value is replaced rather than validated. Anything
        else is honoured if the submitter is entitled to it and capped at their
        standing if not.

        ``on_behalf_of`` first, then the listing's own vendor. An inline-proposed
        vendor is not verified by definition, so the sentinel maps to None and such
        a submission can only be community - after approval they can re-validate at
        vendor level once the vendor is published and verified.
        """
        vendor = cleaned.get("on_behalf_of") or cleaned.get("vendor")
        if vendor == INLINE_VENDOR_SENTINEL:
            vendor = None
        return resolve_claimed_level(
            self.user, vendor=vendor,
            claimed=cleaned.get("claimed_validation_level") or "",
        )

    # --- Persistence --------------------------------------------------------
    @transaction.atomic
    def save(self) -> Submission:
        if self.submission is not None:
            return self._save_revision()
        model: type[HardwareListing] = System if self.cleaned_data["kind"] == "system" else Component
        listing = self._create_listing(model)
        submission = self._create_submission(listing)
        self._attach_files(submission)
        self._tag_with_category_values(listing)
        self._attach_proposed_values(listing)
        self._attach_cpus(listing)
        # Recorded on the submission, not re-derived from the listing at approval
        # time. See Submission.cited_releases.
        submission.cited_releases.set(self._attach_release_versions(listing))
        return submission

    @staticmethod
    def _initial_from(submission: Submission) -> dict:
        """Pre-fill from the draft this submission created."""
        listing = submission.listing
        initial = {
            "kind": "system" if isinstance(listing, System) else "component",
            "name": listing.name,
            "model_number": listing.model_number,
            "description": listing.description,
            "vendor_spec_url": listing.vendor_spec_url,
            "vendor": listing.vendor.slug,
            "claimed_validation_level": submission.claimed_validation_level,
            "submitter_notes": submission.submitter_notes,
        }
        if submission.on_behalf_of_id is not None:
            initial["on_behalf_of"] = submission.on_behalf_of.slug
        # Tick the releases the submission already claims, so a revision does not
        # silently drop them by rendering an empty set of checkboxes.
        for version in listing.versions.select_related("release"):
            initial[f"{RELEASE_SUPPORT_PREFIX}{version.release.major}"] = True
        return initial

    def _save_revision(self) -> Submission:
        """Apply a revision to the draft and put the same row back in the queue."""
        submission = self.submission
        listing = submission.listing
        copied = ("name", "model_number", "description", "vendor_spec_url")
        for field in copied:
            setattr(listing, field, (self.cleaned_data.get(field) or "").strip())
        listing.save(update_fields=list(copied))

        submission.claimed_validation_level = self.cleaned_data[
            "claimed_validation_level"
        ]
        submission.submitter_notes = self.cleaned_data.get("submitter_notes", "")
        submission.save(
            update_fields=["claimed_validation_level", "submitter_notes"]
        )

        self._attach_files(submission)
        self._tag_with_category_values(listing)
        self._attach_proposed_values(listing)
        self._attach_cpus(listing)
        # Replaces rather than adds, unlike a run's listing proposal, which is
        # deliberately additive because a run is *evidence* and evidence must not be
        # removed by a later submitter. A revision is one person correcting their own
        # unpublished claim because a reviewer asked them to, so unticking a release has
        # to actually untick it.
        #
        # Both writes below are restricted to ``source='declared'`` rows, which is the
        # whole point. A ``run`` row on this listing was put there by
        # ``record_compatibility`` from a passing bundle, and only that function may promote a
        # declared row to proven - which is exactly what deleting the re-validation flow was
        # about. Re-introducing a way to edit one through a form here would put it straight back.
        claimed = self._attach_release_versions(listing)
        declared = listing.versions.filter(source=ListingVersion.SOURCE_DECLARED)
        declared.exclude(release__in=claimed).delete()
        # No per-row update after the get_or_create: a row used to carry a minor floor a
        # revision could correct, and a major carries nothing beyond its own existence.
        submission.cited_releases.set(claimed)

        submission.resubmit()
        return submission

    # -------------------------------- internals --------------------------------
    def _resolve_inline_vendor(self) -> Vendor:
        """Materialize the inline-proposed vendor as a draft (published=False).

        Ownership is deliberately not granted here - see
        ``vendors.services.create_inline_vendor`` for why the submitter only gets
        submit rights.
        """
        return create_inline_vendor(
            name=self.cleaned_data["new_vendor_name"],
            created_by=self.user,
            scope=Vendor.SCOPE_HARDWARE,
            homepage=self.cleaned_data.get("new_vendor_homepage") or "",
            contact_email=self.cleaned_data.get("new_vendor_contact_email") or "",
            description=self.cleaned_data.get("new_vendor_description") or "",
            logo=self.cleaned_data.get("new_vendor_logo") or None,
        )

    def _create_listing(self, model: type[HardwareListing]) -> HardwareListing:
        """Always a fresh draft. Never an existing listing.

        This form cannot address a listing it did not create, which is what makes
        ``_attach_release_versions`` safe to call from ``save()``: the only rows it can
        touch are ones that did not exist a moment ago.
        """
        vendor = self.cleaned_data["vendor"]
        if vendor == INLINE_VENDOR_SENTINEL:
            vendor = self._resolve_inline_vendor()
        on_behalf = self.cleaned_data.get("on_behalf_of")
        # If the submitter inlined the vendor, the vendor itself becomes the
        # owner - they're claiming maintenance of the listing too.
        owner = on_behalf or (vendor if not vendor.published else None)
        return model.objects.create(
            name=self.cleaned_data["name"],
            model_number=self.cleaned_data.get("model_number", ""),
            description=self.cleaned_data.get("description", ""),
            vendor_spec_url=self.cleaned_data.get("vendor_spec_url", ""),
            vendor=vendor,
            owner_vendor=owner,
            created_by=self.user,
        )

    def _create_submission(self, listing: HardwareListing) -> Submission:
        fk = "listing_system" if isinstance(listing, System) else "listing_component"
        return Submission.objects.create(
            submitter=self.user,
            on_behalf_of=self.cleaned_data.get("on_behalf_of"),
            claimed_validation_level=self.cleaned_data["claimed_validation_level"],
            submitter_notes=self.cleaned_data.get("submitter_notes", ""),
            **{fk: listing},
        )

    def _attach_files(self, submission: Submission) -> None:
        """Store each uploaded file with its digest.

        ``sha256`` had no writer anywhere in the project, so every attachment stored
        an empty one. It is not integrity in the sense the bundle path means it -
        nothing here is checked against a manifest, and a declared submission has no
        manifest to check against - but it makes an attachment's bytes identifiable
        after the fact, which is the least a reviewer needs to be able to say "this is
        the file I looked at".
        """
        for f in self.files.getlist("attachments"):
            TestResultAttachment.objects.create(
                submission=submission, file=f, sha256=hash_upload(f),
            )

    def _tag_with_category_values(self, listing: HardwareListing) -> None:
        fk = "listing_system" if isinstance(listing, System) else "listing_component"
        for category, cat_field, _propose_field in self._category_fields:
            raw = self.cleaned_data.get(cat_field)
            # ChoiceField returns "" or a single slug; MultipleChoiceField
            # returns a list. Normalize so the loop body is one shape.
            slugs = [raw] if isinstance(raw, str) else list(raw or [])
            for value_slug in slugs:
                if not value_slug:
                    continue
                try:
                    cv = CategoryValue.objects.get(
                        category=category, slug=value_slug,
                        status=CategoryValue.STATUS_APPROVED,
                    )
                except CategoryValue.DoesNotExist:
                    continue
                ListingCategoryValue.objects.get_or_create(value=cv, **{fk: listing})

    def _attach_proposed_values(self, listing: HardwareListing) -> None:
        fk = "listing_system" if isinstance(listing, System) else "listing_component"
        for category, _cat_field, propose_field in self._category_fields:
            if propose_field is None:
                continue  # category disallows suggestions
            raw = (self.cleaned_data.get(propose_field) or "").strip()
            if not raw:
                continue
            cv = CategoryValue.propose(category=category, value=raw, proposed_by=self.user)
            ListingCategoryValue.objects.create(value=cv, **{fk: listing})

    def _attach_cpus(self, listing: HardwareListing) -> None:
        if not isinstance(listing, System):
            return
        for cpu_pk in self.cleaned_data.get("cpus", []) or []:
            cpu = Component.objects.filter(pk=cpu_pk).first()
            if cpu is None:
                continue
            attach_cpu(listing, cpu)
        # Inline new-CPU rows: each non-empty (name + vendor) row becomes a
        # draft Component(kind=cpu, published=False) attached to the system.
        # Reviewer approval cascade-publishes them.
        for name_f, vendor_f, desc_f in self._new_cpu_rows:
            name = (self.cleaned_data.get(name_f) or "").strip()
            vendor_slug = self.cleaned_data.get(vendor_f) or ""
            if not name or not vendor_slug:
                continue
            vendor = Vendor.objects.filter(slug=vendor_slug).first()
            if vendor is None:
                continue
            cpu = Component.objects.create(
                name=name,
                vendor=vendor,
                description=self.cleaned_data.get(desc_f) or "",
                kind=ComponentKind.cpu.value,
                published=False,
            )
            attach_cpu(listing, cpu)

    def _attach_release_versions(self, listing: HardwareListing) -> list[AlmaLinuxRelease]:
        """Create ListingVersion rows for each release the submitter ticked.

        ``get_or_create``, not ``update_or_create``. The listing is always brand new
        (see ``_create_listing``) so no row can pre-exist and the two behave alike
        here, but the distinction is the whole defect this form used to carry: an
        update keyed on (listing, release) silently overwrote what a validation run had
        established - back then a ``minimum_minor`` floor, and today the row's ``source``.
        Nothing outside ``record_compatibility`` should mark a release as proven.

        Returns the releases actually cited, which the caller records on the
        Submission. ``Submission.approve`` needs them: reading them back off the
        listing at approval time is what let a submission attest releases it never
        mentioned.
        """
        fk_kw = listing_fk(listing)
        cited: list[AlmaLinuxRelease] = []
        for release, support_f in self._release_fields:
            if not self.cleaned_data.get(support_f):
                continue
            ListingVersion.objects.get_or_create(release=release, **fk_kw)
            cited.append(release)
        return cited
