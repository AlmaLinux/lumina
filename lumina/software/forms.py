"""Software catalog forms."""
from __future__ import annotations

from django import forms
from django.db import transaction

from lumina.core.certification import ValidationLevel
from lumina.core.forms import bootstrapify, narrow_level_field
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareCategoryValue,
    SoftwareCompatibility,
    SoftwareEditProposal,
    SoftwareEvidenceAttachment,
    SoftwareSubmission,
)
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor
from lumina.vendors.services import (
    create_inline_vendor,
    resolve_claimed_level,
    vendors_for_submission,
)


class SoftwareEditProposalForm(forms.ModelForm):
    """Pre-filled with current values; blank means "no change" at approval."""

    class Meta:
        model = SoftwareEditProposal
        fields = (
            "name", "description", "homepage_url", "documentation_url",
            "support_url", "submitter_notes",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "submitter_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, software: Software, **kwargs):
        kwargs.setdefault("initial", {}).update(
            name=software.name,
            description=software.description,
            homepage_url=software.homepage_url,
            documentation_url=software.documentation_url,
            support_url=software.support_url,
        )
        super().__init__(*args, **kwargs)
        bootstrapify(self)


CATEGORY_FIELD_PREFIX = "cat_"
PROPOSE_FIELD_PREFIX = "propose_"
RELEASE_SUPPORT_PREFIX = "release_support_"
INLINE_VENDOR_SENTINEL = "__new__"


class SoftwareSubmissionForm(forms.Form):
    """One form for a new listing and for re-validating an existing one.

    Mirrors ``hardware.forms.SubmissionForm``'s prefix-constant contract so the
    template stays dumb, minus the whole minor-version block: software cites
    AlmaLinux **majors** only, so this is a checkbox per supported release with no
    ``release_min_minor_<major>`` companion.

    One guard lives here rather than at the storage layer, because a form can
    explain itself and an IntegrityError cannot: at least one cited AlmaLinux
    major, since a certification naming no release certifies nothing.
    """

    software_slug = forms.SlugField(required=False)
    name = forms.CharField(max_length=200, required=False)
    vendor = forms.ChoiceField(
        choices=[], widget=forms.Select(attrs={
            # A searchable list, not a native dropdown: the publisher list is
            # unbounded and will run to thousands, where a <select> means scrolling
            # and the browser's prefix-only type-ahead. combobox.js replaces the
            # picking; the select still holds the value, so server-side validation
            # and no-JS use are untouched.
            "data-combobox": "true",
            "data-placeholder": "Search publishers\u2026",
            # The menu caps at twelve entries, and "+ Add a new publisher" is last in
            # the option list, so without pinning it falls off the bottom the moment
            # there are more than twelve vendors and the inline flow becomes
            # undiscoverable.
            "data-combobox-pin": INLINE_VENDOR_SENTINEL,
            # Picking the action scrolls to and focuses the fields it unlocks.
            # Without this it set a hidden value and closed the menu, so from the
            # submitter's side nothing happened.
            "data-combobox-pin-target": "#id_new_vendor_name",
        }),
    )
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), required=False
    )
    homepage_url = forms.URLField(required=False, assume_scheme="https")
    documentation_url = forms.URLField(required=False, assume_scheme="https")
    support_url = forms.URLField(required=False, assume_scheme="https")

    new_vendor_name = forms.CharField(max_length=120, required=False)
    new_vendor_homepage = forms.URLField(required=False, assume_scheme="https")
    new_vendor_contact_email = forms.EmailField(required=False)
    new_vendor_description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    on_behalf_of = forms.ChoiceField(choices=[], required=False)
    claimed_validation_level = forms.ChoiceField(choices=ValidationLevel.choices)
    submitter_notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), required=False
    )
    evidence = forms.FileField(required=False)

    def __init__(self, *args, user, submission: SoftwareSubmission | None = None,
                 **kwargs):
        self.user = user
        # Revise mode: edit the draft this submission already created instead of
        # creating a new listing and a new submission row.
        self.submission = submission
        if submission is not None:
            kwargs.setdefault("initial", {}).update(
                self._initial_from(submission)
            )
        super().__init__(*args, **kwargs)

        software_vendors = Vendor.objects.published().for_scope(Vendor.SCOPE_SOFTWARE)
        self.fields["vendor"].choices = (
            [("", "---------")]
            + [(v.slug, v.name) for v in software_vendors.order_by("name")]
            + [(INLINE_VENDOR_SENTINEL, "+ Add a new publisher…")]
        )
        self.fields["on_behalf_of"].choices = [("", "---------")] + [
            (v.slug, v.name) for v in vendors_for_submission(user).order_by("name")
        ]
        narrow_level_field(self, self.user)

        # One checkbox per supported release. No minor control at all - that is
        # the bulk of hardware's release block and software does not use it.
        self._release_fields: list[tuple[AlmaLinuxRelease, str]] = []
        for release in AlmaLinuxRelease.objects.supported():
            field_name = f"{RELEASE_SUPPORT_PREFIX}{release.major}"
            self.fields[field_name] = forms.BooleanField(
                required=False, label=str(release)
            )
            self._release_fields.append((release, field_name))

        self._category_fields: list[tuple[Category, str, str | None]] = []
        for category in Category.objects.filter(
            applies_to=Category.APPLIES_SOFTWARE
        ).order_by("display_order", "name"):
            approved = list(category.values.filter(
                status=CategoryValue.STATUS_APPROVED
            ))
            field_name = f"{CATEGORY_FIELD_PREFIX}{category.slug}"
            self.fields[field_name] = forms.MultipleChoiceField(
                required=False, label=category.name,
                choices=[(v.slug, v.value) for v in approved],
                widget=forms.CheckboxSelectMultiple,
            )
            propose_name = None
            if category.allow_suggestions:
                propose_name = f"{PROPOSE_FIELD_PREFIX}{category.slug}"
                self.fields[propose_name] = forms.CharField(
                    required=False, label=f"Propose a new {category.name} value",
                )
            self._category_fields.append((category, field_name, propose_name))

        bootstrapify(self)

    @staticmethod
    def _initial_from(submission: SoftwareSubmission) -> dict:
        """Pre-fill from the draft this submission created.

        The dynamic field names are computed rather than read off
        ``self._release_fields``, because this runs before ``__init__`` has built
        them - ``initial`` has to be in ``kwargs`` by the time ``super().__init__``
        sees it.
        """
        software = submission.software
        initial: dict = {
            "software_slug": software.slug,
            "name": software.name,
            "vendor": software.vendor.slug,
            "description": software.description,
            "homepage_url": software.homepage_url,
            "documentation_url": software.documentation_url,
            "support_url": software.support_url,
            "claimed_validation_level": submission.claimed_validation_level,
            "submitter_notes": submission.submitter_notes,
        }
        if submission.on_behalf_of_id is not None:
            initial["on_behalf_of"] = submission.on_behalf_of.slug
        for row in software.compatibility.select_related("release"):
            initial[f"{RELEASE_SUPPORT_PREFIX}{row.release.major}"] = True
        for binding in software.category_values.select_related("value__category"):
            key = f"{CATEGORY_FIELD_PREFIX}{binding.value.category.slug}"
            initial.setdefault(key, []).append(binding.value.slug)
        return initial

    @property
    def release_field_groups(self) -> list[dict]:
        return [
            {"release": release, "bound_field": self[name]}
            for release, name in self._release_fields
        ]

    @property
    def category_field_groups(self) -> list[dict]:
        return [
            {
                "category": category,
                "bound_field": self[name],
                "propose_bound_field": self[propose] if propose else None,
            }
            for category, name, propose in self._category_fields
        ]

    def clean_vendor(self):
        value = self.cleaned_data.get("vendor")
        if value == INLINE_VENDOR_SENTINEL:
            # Cannot materialize yet: clean_* runs before clean(), which is where
            # the new vendor's name is validated.
            return value
        return Vendor.objects.filter(slug=value).first()

    def clean_on_behalf_of(self):
        slug = self.cleaned_data.get("on_behalf_of")
        if not slug:
            return None
        vendor = vendors_for_submission(self.user).filter(slug=slug).first()
        if vendor is None:
            raise forms.ValidationError(
                "You do not have submit rights for that vendor."
            )
        return vendor

    def clean(self):
        cleaned = super().clean()
        # Attribution decides the tier - see resolve_claimed_level. Replaces the
        # posted value rather than validating it, so the dropdown never has to
        # offer an option that contradicts the vendor selection beside it.
        cleaned["claimed_validation_level"] = resolve_claimed_level(
            self.user, vendor=cleaned.get("on_behalf_of"),
            claimed=cleaned.get("claimed_validation_level") or "",
        )

        if not any(
            cleaned.get(name) for _, name in self._release_fields
        ):
            self.add_error(
                None,
                "Pick at least one AlmaLinux release. A certification that names "
                "no release certifies nothing.",
            )

        if cleaned.get("vendor") == INLINE_VENDOR_SENTINEL:
            name = (cleaned.get("new_vendor_name") or "").strip()
            if not name:
                self.add_error("new_vendor_name", "Name the vendor you are proposing.")
            elif Vendor.objects.filter(name__iexact=name).exists():
                self.add_error(
                    "new_vendor_name",
                    "A vendor with that name already exists - pick it from the list.",
                )

        if not cleaned.get("software_slug") and not (cleaned.get("name") or "").strip():
            self.add_error("name", "Name the product.")

        return cleaned

    @transaction.atomic
    def save(self) -> SoftwareSubmission:
        if self.submission is not None:
            return self._save_revision()
        software = self._resolve_or_create_software()
        submission = SoftwareSubmission.objects.create(
            submitter=self.user,
            on_behalf_of=self.cleaned_data.get("on_behalf_of"),
            software=software,
            claimed_validation_level=self.cleaned_data["claimed_validation_level"],
            submitter_notes=self.cleaned_data.get("submitter_notes", ""),
        )
        if self.cleaned_data.get("evidence"):
            SoftwareEvidenceAttachment.objects.create(
                submission=submission, file=self.cleaned_data["evidence"],
            )
        self._attach_categories(software)
        self._attach_proposed_values(software)
        self._attach_majors(software)
        return submission

    def _save_revision(self) -> SoftwareSubmission:
        """Apply a revision to the draft and put the same row back in the queue."""
        submission = self.submission
        software = submission.software
        copied = (
            "name", "description", "homepage_url", "documentation_url",
            "support_url",
        )
        for field in copied:
            setattr(software, field, (self.cleaned_data.get(field) or "").strip())
        software.save(update_fields=list(copied))

        submission.claimed_validation_level = self.cleaned_data[
            "claimed_validation_level"
        ]
        submission.submitter_notes = self.cleaned_data.get("submitter_notes", "")
        submission.save(update_fields=[
            "claimed_validation_level", "submitter_notes"
        ])
        if self.cleaned_data.get("evidence"):
            SoftwareEvidenceAttachment.objects.create(
                submission=submission, file=self.cleaned_data["evidence"],
            )

        self._sync_categories(software)
        self._attach_proposed_values(software)
        self._sync_majors(software)
        submission.resubmit()
        return submission

    def _resolve_inline_vendor(self) -> Vendor:
        return create_inline_vendor(
            name=self.cleaned_data["new_vendor_name"],
            created_by=self.user,
            scope=Vendor.SCOPE_SOFTWARE,
            homepage=self.cleaned_data.get("new_vendor_homepage") or "",
            contact_email=self.cleaned_data.get("new_vendor_contact_email") or "",
            description=self.cleaned_data.get("new_vendor_description") or "",
        )

    def _resolve_or_create_software(self) -> Software:
        slug = self.cleaned_data.get("software_slug")
        if slug:
            return Software.objects.get(slug=slug)
        vendor = self.cleaned_data["vendor"]
        if vendor == INLINE_VENDOR_SENTINEL:
            vendor = self._resolve_inline_vendor()
        on_behalf = self.cleaned_data.get("on_behalf_of")
        # An unpublished vendor is an inline proposal, so the submitter is
        # claiming maintenance of the listing too; a published one is not.
        owner = on_behalf or (vendor if not vendor.published else None)
        return Software.objects.create(
            vendor=vendor,
            owner_vendor=owner,
            name=self.cleaned_data["name"].strip(),
            description=self.cleaned_data.get("description", ""),
            homepage_url=self.cleaned_data.get("homepage_url", ""),
            documentation_url=self.cleaned_data.get("documentation_url", ""),
            support_url=self.cleaned_data.get("support_url", ""),
            published=False,
            created_by=self.user,
        )

    def _attach_categories(self, software: Software) -> None:
        for _category, field_name, _propose in self._category_fields:
            for value_slug in self.cleaned_data.get(field_name) or []:
                value = CategoryValue.objects.approved().filter(
                    slug=value_slug, category__slug=field_name[len(CATEGORY_FIELD_PREFIX):]
                ).first()
                if value is not None:
                    SoftwareCategoryValue.objects.get_or_create(
                        software=software, value=value
                    )

    def _attach_proposed_values(self, software: Software) -> None:
        for category, _field_name, propose_name in self._category_fields:
            if propose_name is None:
                continue
            text = (self.cleaned_data.get(propose_name) or "").strip()
            if not text:
                continue
            # Bind an existing value rather than skipping the box entirely. The
            # submitter asked for this value; that it already exists (perhaps
            # because they proposed it on the submission being revised) is a
            # reason to reuse it, not to drop the request on the floor.
            value = category.values.filter(value__iexact=text).first()
            if value is None:
                value = CategoryValue.propose(
                    category=category, value=text, proposed_by=self.user,
                )
            # Bound immediately, but invisible publicly until a reviewer promotes
            # it - the same arrangement hardware uses.
            SoftwareCategoryValue.objects.get_or_create(software=software, value=value)

    def _sync_categories(self, software: Software) -> None:
        """Make the bindings match the boxes, adding and removing.

        Revision-only: a submitter told to drop a wrong category needs the
        unticked box to mean something.
        """
        keep: set[int] = set()
        for _category, field_name, _propose in self._category_fields:
            slug = field_name[len(CATEGORY_FIELD_PREFIX):]
            for value_slug in self.cleaned_data.get(field_name) or []:
                value = CategoryValue.objects.approved().filter(
                    slug=value_slug, category__slug=slug
                ).first()
                if value is not None:
                    SoftwareCategoryValue.objects.get_or_create(
                        software=software, value=value
                    )
                    keep.add(value.pk)
        # Scoped to the categories this form actually offers, so a binding to a
        # category outside the software scope is left alone rather than silently
        # discarded by a form that never showed it.
        offered = [category.pk for category, _f, _p in self._category_fields]
        software.category_values.filter(
            value__category__in=offered
        ).exclude(value__in=keep).exclude(
            # A value still awaiting review is not in `approved()`, so it can
            # never land in `keep`. Dropping it here would delete the proposal
            # `_attach_proposed_values` just made.
            value__status=CategoryValue.STATUS_PENDING
        ).delete()

    def _sync_majors(self, software: Software) -> None:
        """Make the cited majors match the checkboxes, adding and removing.

        Revision-only, and the removal is the point: "you cited the wrong
        release" is otherwise unfixable.

        A row carrying somebody else's evidence is never removed. On an
        unpublished draft there is none, but the same form serves re-validation of
        a published listing, where a community member may have reported or
        confirmed a major - and a vendor unticking a box must not delete that.
        """
        wanted = {
            release.major
            for release, field_name in self._release_fields
            if self.cleaned_data.get(field_name)
        }
        for release, field_name in self._release_fields:
            if self.cleaned_data.get(field_name):
                SoftwareCompatibility.objects.get_or_create(
                    software=software, release=release,
                )
        for row in software.compatibility.select_related("release"):
            if row.release.major in wanted:
                continue
            if row.certifications.exists() or row.attestations.exists():
                continue
            row.delete()

    def _attach_majors(self, software: Software) -> None:
        """Write the cited majors now, not at approval.

        The submission row carries no list of them, so the draft listing has to be
        where a reviewer reads them from. ``get_or_create`` because a community
        member may already have reported one of these majors, and that pending row
        should be reused rather than collide with unique(software, release).
        """
        for release, field_name in self._release_fields:
            if self.cleaned_data.get(field_name):
                SoftwareCompatibility.objects.get_or_create(
                    software=software, release=release,
                )
