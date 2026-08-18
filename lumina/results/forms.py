"""Forms for manual bundle upload and reviewer actions on runs."""
from __future__ import annotations

from django import forms
from django.conf import settings
from django.utils.html import format_html, format_html_join

from lumina.core.certification import ValidationLevel
from lumina.core.forms import bootstrapify
from lumina.hardware.models import Component, System
from lumina.results import proposal_keys


class ComboBoxInput(forms.TextInput):
    """Free-text field backed by a list of values already in the catalog.

    Free text is the point, not a fallback: hardware this catalog has never
    seen has to be typeable, and the entire reason this form exists is that the
    collected data is sometimes wrong. The list is a shortcut and a nudge
    toward reusing an existing entry instead of creating a near-duplicate
    ("Dell" beside "Dell Inc."), never a constraint.

    Renders a native ``<datalist>``, so matching works with no JavaScript at
    all. ``combobox.js`` then takes over to make the matching consistent across
    browsers (substring rather than prefix) and to show the whole list on
    focus; it removes the ``list`` attribute when it does, so the two never
    appear at once.
    """

    def __init__(self, options=(), attrs=None):
        self.options = list(options)
        super().__init__(attrs)

    def render(self, name, value, attrs=None, renderer=None):
        list_id = f"combo-{name}"
        attrs = {
            **(attrs or {}),
            "list": list_id,
            "data-combobox": "true",
            # Browser autofill over a catalog field offers the user's own
            # address history, which is never a vendor name.
            "autocomplete": "off",
        }
        field = super().render(name, value, attrs, renderer)
        options = format_html_join(
            "", '<option value="{}"></option>', ((option,) for option in self.options)
        )
        return format_html(
            '{}<datalist id="{}">{}</datalist>', field, list_id, options
        )


class BundleUploadForm(forms.Form):
    """Offline submission path: the same bundle the CLI would have POSTed."""

    bundle = forms.FileField(
        label="Result bundle",
        help_text="The .tar.zst produced by `alma-cert bundle` "
                  "(.tar.gz from older suite versions also works).",
    )
    pre_release = forms.BooleanField(
        required=False,
        label="Unreleased hardware",
        help_text="Withhold these results from public view until the date below.",
    )
    publish_after = forms.DateField(
        required=False,
        label="Publish on or after",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Approved results stay completely private until this date.",
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Notes for the reviewer",
    )

    def clean_bundle(self):
        bundle = self.cleaned_data["bundle"]
        limit = settings.LUMINA_BUNDLE_MAX_BYTES
        if bundle.size > limit:
            raise forms.ValidationError(
                f"Bundle is {bundle.size // (1024 * 1024)} MB; "
                f"the limit is {limit // (1024 * 1024)} MB."
            )
        return bundle

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("publish_after") and not cleaned.get("pre_release"):
            # A publish date only means something for embargoed submissions;
            # silently ignoring it would surprise the submitter.
            self.add_error(
                "pre_release",
                "Tick this to embargo the results until the publish date.",
            )
        return cleaned


class ComponentTiesMixin:
    """The component-tie half of a run form: what gets tied, and correcting it.

    Shared by the submitter's ``RunListingProposalForm`` and the reviewer's
    ``RunComponentTiesForm``, because it is the same job from two seats. The submitter is
    holding the machine; the reviewer is looking at the whole submission. Both can see
    that DMI reported "OEM" for a whitebox board, and both need the same three controls:
    keep or drop the tie, and correct its vendor and model.

    Written once for the same reason ``component_tie_targets`` is the single source for
    the preview and for the tie itself: a second copy of "what would approving do" is how
    a preview starts lying. ``ensure_component_ties`` was that second copy until it was
    folded back, and every feature since had had to be written twice.
    """

    # ``included_ties`` and the per-component boxes are all created in
    # ``_build_component_rows`` rather than declared here, and that is not a style choice.
    # Django's form metaclass collects declared fields only from bases that already carry
    # ``declared_fields`` - that is, from other form classes - so a field declared on a
    # plain mixin is an ordinary class attribute and never reaches ``self.fields``. It
    # silently did not exist. Everything here depends on the run anyway, so building it all
    # in one place is the honest shape.

    def _build_component_rows(self) -> None:
        """Offer the reported components, each with its catalog status and an opt-out.

        The submitter is the only person who can see the machine, so they are the one who
        knows that the GPU is a spare card that came with the chassis, or that the
        onboard graphics are not part of what they are certifying. Until now nobody could
        say so: the ties were re-derived from the report every time, and a reviewer
        clearing the component list watched all of it come back at approval.

        The same ``preview_component_ties`` the reviewer's page reads, so the two cannot
        drift into disagreeing about what approving would do.
        """
        self.component_rows = []
        if self.run is None:
            return

        from lumina.results.services import preview_component_ties

        entries = preview_component_ties(self.run)
        if not entries:
            return

        # An **include** list: every box starts ticked and unticking one drops that part.
        #
        # Posted the other way round at first - checked meant excluded - which read
        # backwards against copy saying "untick anything that is not part of what you are
        # certifying", and against every other checkbox on the page. What gets *stored* is
        # still the exclusion set (``TestRun.excluded_component_ties``), because the
        # exception is the thing worth persisting: a part nobody objected to needs no row
        # saying so, and a stored include list would go stale the moment a re-upload
        # reported a new GPU.
        self.fields[self._control("included_ties")] = forms.MultipleChoiceField(
            required=False, widget=forms.CheckboxSelectMultiple,
            choices=[(entry["key"], entry["key"]) for entry in entries],
        )
        # Was this section of the page actually submitted?
        #
        # Every control here answers by *absence*: an unticked checkbox is not posted at all,
        # which is what makes the include list and the vendor claim work. The cost is that a
        # request which never rendered this section is indistinguishable from one where the
        # reader unticked everything - so a partial post silently excluded every component and
        # recorded a declined vendor claim on each one. Reported as the component boxes not
        # being there; found on a devstack run where the offender was a verification script of
        # mine posting only the identity fields.
        #
        # A hidden marker separates the two. ``clean`` already applies the same rule to the
        # locked release minors ("a request that omits it cleans to 0"), so this is that
        # precedent applied to a whole section rather than a new idea.
        self.fields[self._control(self.COMPONENTS_MARKER)] = forms.CharField(
            required=False, widget=forms.HiddenInput, initial="1",
        )
        # Ticked by default, and a saved exclusion is what unticks one.
        if not self.is_bound:
            self.initial.setdefault(
                "included_ties",
                [
                    entry["key"] for entry in entries if not entry["excluded"]
                ],
            )
        self.component_rows = entries

    # Rendered inside the components card. Its presence in the posted data is what says the
    # reader was shown these controls, so an untick means something.
    COMPONENTS_MARKER = "components_submitted"

    def _components_were_submitted(self) -> bool:
        return not self.is_bound or self.COMPONENTS_MARKER in self.data

    @property
    def component_groups(self) -> list:
        """``component_rows`` grouped by kind, controls and all.

        So one list can carry both the status a reviewer reads and the controls they act with.
        The review page had two: a grouped read-only preview and a flat editable form over the
        same entries, which meant the same part appeared twice on one page with different
        information beside each copy.
        """
        from lumina.results.services import group_component_rows

        return group_component_rows(self.component_rows)

    def excluded_tie_keys(self) -> list:
        """The ties to *skip*, derived from the boxes left unticked.

        Computed here rather than in the view so the inversion happens in exactly one
        place, and only over the keys this form actually offered - a run whose report
        changed between saves must not have an unrelated stale key resurrected.
        """
        if not self._components_were_submitted():
            return list(self.run.excluded_component_ties or [])
        offered = [key for key, _ in self.fields["included_ties"].choices] \
            if "included_ties" in self.fields else []
        kept = set(self.cleaned_data.get("included_ties") or [])
        return [key for key in offered if key not in kept]

    # Prefix for a vendor entry in the attribution dropdown. Prefixed rather than bare, so
    # a vendor whose slug happens to be "community" cannot collide with the tier options.
    VENDOR_CHOICE_PREFIX = "vendor:"

    # Per-component correction fields, named from the tie key's position in the list rather
    # than from the key itself: a key contains a colon, spaces, and whatever punctuation the
    # firmware felt like, none of which belongs in an HTML form field name.
    # Fields that are *controls*, not listing data. Registered as they are created rather
    # than listed somewhere central, because the central list kept being one name short: five
    # keys have leaked into the stored proposal one at a time - ``attribution``,
    # ``included_ties``, ``tie_claim_*``, ``tie_edit_*``, ``components_submitted`` - each found
    # by reading a stray devstack row rather than by a test. A field registered where it is
    # built cannot be forgotten by whoever adds the next one.
    STATIC_CONTROL_FIELDS = (
        "attribution", "identity_disputed", "submitter_notes",
        # The embargo lives on the run, not in the listing proposal: it governs this evidence,
        # not what the machine is.
        "pre_release", "publish_requested_date", "available_from_minor",
        # Not fields at all: ``clean`` synthesises these out of ``attribution`` and they ride
        # in ``cleaned_data`` alongside the real ones.
        "on_behalf_of", "claimed_validation_level",
    )

    def _control(self, name: str) -> str:
        """Mark ``name`` as a control field and return it, for use inline where it is built."""
        if not hasattr(self, "_control_fields"):
            self._control_fields = set(self.STATIC_CONTROL_FIELDS)
        self._control_fields.add(name)
        return name

    def listing_proposal_data(self) -> dict:
        """``cleaned_data`` minus everything that is not a fact about the listing."""
        if not hasattr(self, "_control_fields"):
            self._control_fields = set(self.STATIC_CONTROL_FIELDS)
        return {
            key: value for key, value in self.cleaned_data.items()
            if key not in self._control_fields
        }

    COMPONENT_BRAND_PREFIX = "tie_brand_"
    COMPONENT_MODEL_PREFIX = "tie_model_"
    COMPONENT_CLAIM_PREFIX = "tie_claim_"
    # "The catalog matched this part to the wrong entry", per row. The component-level twin of
    # the identity override, for the same reason: a part the catalog already holds is described
    # by its existing entry, and offering two text boxes over the top invites a near-duplicate
    # where the reader only meant to confirm a match.
    COMPONENT_EDIT_PREFIX = "tie_edit_"

    def _build_component_fields(self) -> None:
        """A vendor and model box per reported component, prefilled and suggestible.

        The report is frequently wrong about these. DMI says "OEM" for a whitebox board and
        lspci says "CometLake-S GT2 [UHD Graphics 630]" for a UHD Graphics 630; left alone,
        the first becomes a catalog manufacturer named OEM and the second a component nobody
        will ever search for. The submitter is holding the machine and the reviewer can see
        the whole submission, so both get to correct it.

        Suggestions come from the catalog, scoped to the kind being corrected - board names
        for a board, CPU names for a CPU - because the point is to *reuse* an existing entry
        rather than mint a near-duplicate beside it. Free text either way: hardware the
        catalog has never seen has to be typeable.
        """
        self._component_fields = []
        self._component_claims = []
        self._component_edits = []
        for index, entry in enumerate(self.component_rows):
            brand_name = self._control(f"{self.COMPONENT_BRAND_PREFIX}{index}")
            model_name = self._control(f"{self.COMPONENT_MODEL_PREFIX}{index}")
            self.fields[brand_name] = forms.CharField(
                max_length=120, required=False, label="Vendor",
                widget=ComboBoxInput(self._vendor_names()),
            )
            self.fields[model_name] = forms.CharField(
                max_length=200, required=False, label="Model",
                widget=ComboBoxInput(self._component_names(entry["kind"])),
            )
            if not self.is_bound:
                self.initial.setdefault(brand_name, entry["brand"])
                self.initial.setdefault(model_name, entry["raw_model"])
            entry["brand_field"] = self[brand_name]
            entry["model_field"] = self[model_name]
            self._component_fields.append((entry["key"], brand_name, model_name))
            self._add_component_edit(index, entry)
            self._add_component_claim(index, entry)

    def _add_component_edit(self, index: int, entry: dict) -> None:
        """Lock a part the catalog already holds behind a per-row override.

        Suggest the match, do not offer the boxes. The same rule as the machine's identity, and
        it belongs here for the same reason: two prefilled text boxes over an entry that
        already exists read as an invitation to retype it, and one stray character mints
        "Intel Core i3-10100T " beside the real thing.

        Only a *matched* row locks. A part the catalog has never seen has to be typeable, and
        the boxes are the whole point there - so is the case where the reader already corrected
        this row, which stays open so they can keep editing or undo by retyping what the report
        said.

        The claim checkbox is untouched by this. A component vendor still gets prompted, still
        ticked by default, because certifying a part the catalog already holds is exactly the
        normal case: the entry is right and their validation is what is new.
        """
        entry["locked"] = bool(entry.get("component")) and not entry.get("overridden")
        if not entry["locked"]:
            return
        name = self._control(f"{self.COMPONENT_EDIT_PREFIX}{index}")
        self.fields[name] = forms.BooleanField(required=False)
        entry["edit_field"] = self[name]
        self._component_edits.append((entry["key"], name))

    def _add_component_claim(self, index: int, entry: dict) -> None:
        """Offer "certify this part as its own vendor" where that claim is available.

        **This is where a component's validation level is set.** There was nowhere: a
        component's tier is derived entirely from its attestations by
        ``recompute_listing_levels``, the tier on a run applied only to the machine, and the
        ``validation_level`` field in the Django admin is a trap - writable, then overwritten
        by the next recompute.

        Offered per part rather than as one choice for the run, because that is the shape of
        the claim. Intel can say "this Xeon family is vendor-validated"; they cannot say
        anything about the Dell chassis it sat in. A single "Validating as: Intel" at the top
        of a form about a Dell system read as the latter, which is why it moved here.

        Eligibility is the **run's submitter**, not whoever is looking at the form. A reviewer
        ticking this for a submitter who does not represent Intel would produce a community
        attestation anyway - ``effective_level`` re-derives it from the submitter's standing -
        so offering it to them would be a control that silently does nothing.
        """
        # The same predicate the engine uses to read an unanswered box as a claim. Two copies of
        # this rule is how the form came to show "Certify as Intel" ticked on a run whose engine
        # recorded no claim at all.
        from lumina.results.services import claimable_vendor_for

        component = entry.get("component")
        vendor = claimable_vendor_for(self.run, component)
        if vendor is None:
            return

        name = self._control(f"{self.COMPONENT_CLAIM_PREFIX}{index}")
        self.fields[name] = forms.BooleanField(
            required=False,
            label=f"Certify as {vendor.name}",
            help_text=(
                f"Records this as {vendor.name}'s own validation of "
                f"{component}. Only this part; the rest of the machine is unaffected."
            ),
        )
        if not self.is_bound:
            # Ticked by default. If you speak for the company that made this part and the run
            # exercised it, their certification is the natural claim - the same reasoning that
            # preselects a machine's own vendor in "Validating as".
            #
            # An explicit decline has to survive, though, so "never asked" and "asked and
            # declined" cannot both be absence. ``component_overrides`` records the decline as
            # an empty ``attribute_to``, and only a key that is absent entirely gets the
            # default. Without that distinction, unticking and saving would come back ticked
            # on the next load and quietly undo itself - the same trap the release checkboxes
            # had.
            stored = (self.run.component_overrides or {}).get(entry["key"], {})
            self.initial.setdefault(
                name,
                stored.get("attribute_to") == vendor.slug
                if "attribute_to" in stored
                else True,
            )
        entry["claim_field"] = self[name]
        entry["claim_vendor"] = vendor
        self._component_claims.append((entry["key"], name, vendor.slug))

    def component_overrides(self) -> dict:
        """The corrections to store, as ``{tie_key: {"brand", "model"}}``.

        Only what actually differs from the report. Echoing every box back would record a
        correction on every component the moment anybody saved the form, which would then
        survive a later re-upload that reported the part correctly.
        """
        if not self._components_were_submitted():
            return dict(self.run.component_overrides or {})
        overrides: dict = {}
        # Rows whose boxes were locked and left locked. Their values are the prefilled match,
        # so recording them would be recording the report back at itself - and a hand-made post
        # naming something else must not slip a correction past a control nobody was shown.
        untouched = {
            key for key, field_name in getattr(self, "_component_edits", [])
            if not self.cleaned_data.get(field_name)
        }
        for key, brand_name, model_name in getattr(self, "_component_fields", []):
            if key in untouched:
                continue
            entry = next(
                (row for row in self.component_rows if row["key"] == key), None,
            )
            if entry is None:
                continue
            brand = (self.cleaned_data.get(brand_name) or "").strip()
            model = (self.cleaned_data.get(model_name) or "").strip()
            stored = (self.run.component_overrides or {}).get(key) or {}
            chosen = {}
            # Three baselines, and which one a value matches decides three different things.
            #
            # The preview prefills the box with the resolved catalog vendor - "NVIDIA" for a
            # reported "NVIDIA Corporation" - because that reads better. Comparing only against
            # the reported string therefore recorded a correction on every part whose vendor name
            # the catalog spells differently, from a reader who touched nothing. It went unnoticed
            # while the collector happened to report the short spellings itself.
            #
            # And once a correction is stored, the prefill *is* that correction, because
            # ``component_tie_targets`` renders the override. So "equals the prefill" cannot mean
            # "record nothing" unconditionally: for an already-corrected part it means the reader
            # left the correction alone, and dropping it there erased the reviewer's own fix at
            # the moment the catalog entry was minted. It was reachable before by saving twice;
            # every approval is now a second save, which turned it from a corner into the
            # ordinary path.
            for value, subkey, prefill, reported in (
                (brand, "brand", entry["brand"], entry["reported_brand"]),
                (model, "model", entry["raw_model"], entry["reported_model"]),
            ):
                if not value:
                    # Cleared. The only reading of an emptied box is "no correction here", and it
                    # is how a reader undoes one without knowing what the report said.
                    continue
                if value == reported:
                    # Typed back what the report said, which is the other way to undo one.
                    continue
                if value == prefill and subkey not in stored:
                    # Echoing an uncorrected prefill: the catalog's spelling, not a correction.
                    continue
                chosen[subkey] = value
            if chosen:
                overrides[key] = chosen
        # The per-part vendor claim rides along in the same dict, so no new column is needed
        # and one place holds everything the submitter said about a component.
        #
        # The decline is written too, as an empty string. The box defaults to ticked, so an
        # absent key means "never asked" and has to be distinguishable from "asked and said
        # no" - otherwise unticking would be forgotten on the next load.
        asked = set()
        for key, field_name, vendor_slug in getattr(self, "_component_claims", []):
            asked.add(key)
            overrides.setdefault(key, {})["attribute_to"] = (
                vendor_slug if self.cleaned_data.get(field_name) else ""
            )

        # A claim this form never asked about is carried forward, not dropped.
        #
        # Reported as a ticked "Certify as Intel" that did not make the CPU family
        # vendor-validated. The submitter's claim was recorded; a *later* save then erased it,
        # because this method rebuilds the whole dict from the fields the current form happens to
        # have. Any save whose claim field was not built - a reviewer's component edit, a form
        # rendered while the vendor was not yet verified, a part that resolved to no component
        # that time - silently revoked a claim nobody meant to touch. The audit trail for run
        # cf9c7c77 shows it exactly: propose_listing records the claim, a component_ties_edit
        # three seconds before approval drops it, and the run certifies at community.
        #
        # Only ``attribute_to``, and only where this form had no claim field for that tie. A box
        # that *was* rendered and left unticked is still a decline: the box defaults to ticked, so
        # unticking is a decision, and it has to survive the next load.
        editable = {
            key for key, _, _ in getattr(self, "_component_fields", [])
        } - untouched
        for key, chosen in (self.run.component_overrides or {}).items():
            if not isinstance(chosen, dict):
                continue
            if "attribute_to" in chosen and key not in asked:
                overrides.setdefault(key, {})["attribute_to"] = chosen["attribute_to"]
            # And the same for a correction whose boxes this form did not render: a locked row, a
            # part that resolved to nothing this time, a form built for a different section of the
            # page. Same reasoning as the claim above - a rebuild from the current form's fields
            # must not revoke an answer the current form never asked about.
            if key in editable:
                continue
            for subkey in ("brand", "model"):
                if subkey in chosen:
                    overrides.setdefault(key, {})[subkey] = chosen[subkey]
        return overrides

    @staticmethod
    def _component_names(kind) -> list:
        """Existing catalog names of one component kind, for the suggestion list."""
        from lumina.hardware.models import Component

        return list(
            Component.objects.filter(kind=getattr(kind, "value", kind))
            .order_by("name")
            .values_list("name", flat=True)
            .distinct()
        )


    @staticmethod
    def _vendor_names() -> list:
        from lumina.vendors.models import Vendor

        return list(
            Vendor.objects.published().order_by("name").values_list("name", flat=True)
        )


class RunListingProposalForm(ComponentTiesMixin, forms.Form):
    """Submitter's review of everything the run reported about its hardware.

    Prefilled from what the suite collected, and every field is editable,
    because DMI is frequently wrong: a vendor ships firmware with "OEM" in the
    manufacturer field, or a machine-type code where the model should be. A
    reviewer still approves the result, so letting the submitter correct their
    own hardware costs nothing and is the only way to fix what the collector
    cannot see.

    The CPU is handled the way the rest of the platform handles CPUs: the
    collector reports a **model** ("AMD EPYC 7343"), that is what gets logged
    because benchmarks are per model, and the family it rolls up to is derived
    for display. A family is only ever chosen by hand when no model was
    detected at all.
    """

    # What the form is describing depends on what the run identified: a vendor
    # system, the motherboard that defines a custom build, or a machine whose
    # firmware named nothing at all.
    SUBJECT_FIELD = {
        "system": {
            "label": "Displayed system name",
            "help": "How this system is shown throughout the catalog and what "
                    "people search for: the human-friendly model name, e.g. "
                    "“PowerEdge R760”. Not a part code - that goes in Model "
                    "number below.",
            "model_help": "The vendor's own part or machine-type code for this "
                          "system, e.g. “21K9001NUS”. Recorded alongside the "
                          "name and never displayed in place of it. Prefilled "
                          "when the firmware reports one.",
        },
        "motherboard": {
            "label": "Displayed motherboard name",
            "help": "How this motherboard is shown throughout the catalog and "
                    "what people search for: the human-friendly board name, "
                    "e.g. “B650M PG Riptide”. On a custom build the board is "
                    "what identifies the machine. Not a part code - that goes "
                    "in Model number below.",
            "model_help": "The vendor's own part number for this motherboard. "
                          "Recorded alongside the name and never displayed in "
                          "place of it. Prefilled when the firmware reports "
                          "one.",
        },
        "machine": {
            "label": "Displayed model name",
            "help": "How this machine is shown throughout the catalog: the "
                    "human-friendly name on the machine or its label, e.g. "
                    "“ThinkSystem SR645”. Its firmware reports none, so this "
                    "has to come from you. Not a part code - that goes in "
                    "Model number below.",
            "model_help": "The vendor's own part or machine-type code, e.g. "
                          "“21K9001NUS”. Recorded alongside the name and never "
                          "displayed in place of it.",
        },
    }

    vendor_name = forms.CharField(
        max_length=120, label="Vendor",
        help_text="Matched against existing vendors (aliases like "
                  "“Dell” vs “Dell Inc.” are handled).",
    )
    name = forms.CharField(max_length=200)
    machine_kind = forms.ChoiceField(
        choices=[
            ("prebuilt", "A vendor-built system, with its own model name"),
            ("custom", "A custom build, identified by its motherboard"),
        ],
        widget=forms.RadioSelect,
        required=False,
        label="What kind of machine is this?",
        help_text="Prefilled from what the firmware reported. Correct it if it "
                  "is wrong: this decides whether the catalog lists a system "
                  "or a motherboard, and the guess is only as good as the "
                  "firmware. A vendor stamping its machine-type code into both "
                  "DMI tables reads as a custom build; a barebones chassis "
                  "with a model name reads as a vendor system.",
    )
    model_number = forms.CharField(
        max_length=120, required=False,
        help_text="The vendor's own part or machine-type code, e.g. Lenovo's "
                  "“21K9001NUS”. Prefilled when DMI reports one.",
    )
    # Listing *maintenance* fields, kept but not offered to everyone. Removed outright at
    # first, which overshot: it left the hardware's own vendor with nowhere to describe
    # their machine, and on a brand-new listing the description genuinely is a new fact
    # relevant to the submission. ``_drop_maintenance_fields`` decides who sees them.
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4}), required=False,
        help_text="What this machine is; anything a buyer checking "
                  "compatibility should know.",
    )
    vendor_spec_url = forms.URLField(
        required=False, label="Vendor spec sheet URL", assume_scheme="https",
    )

    # The specific part, which is what benchmarks rank and what the family is
    # derived from. Prefilled from the run and editable, because lspci and DMI
    # both report strings vendors did not intend as product names.
    cpu_model = forms.CharField(
        max_length=200, required=False, label="CPU model",
        help_text="The specific processor, e.g. “AMD EPYC 7343”. Its family is "
                  "worked out from this for certification; the model itself is "
                  "what benchmark results are recorded against.",
    )
    # Only offered when no model was detected. Populated in __init__ so a
    # family added in the admin appears without a restart.
    cpu_family = forms.ChoiceField(
        choices=[], required=False, label="CPU family",
        help_text="Choose a family only if the exact model is unknown. "
                  "Certification applies to the family either way.",
    )
    # "The catalog matched this run to the wrong machine."
    #
    # A field of this form rather than a separate button, because a separate button had to live
    # somewhere and the only sensible place was inside this form - which made it a nested
    # <form>. Browsers ignore the inner one, so it submitted *this* form instead: the proposal
    # saved with the identity fields absent (blanking them), the page redirected to the run
    # overview, and the flag was never set. Three reported symptoms, one cause.
    #
    # Hidden until the reader asks for it. The identity fields are rendered but collapsed, and
    # the button that reveals them ticks this - so the whole thing is one save with no round
    # trip, which is also what makes the fields fillable at the moment they appear.
    identity_disputed = forms.BooleanField(required=False)

    # One question - "whose validation is this?" - where there used to be two.
    #
    # "Submitting on behalf of" and "Validation level" were separate dropdowns, and they
    # were not independent: naming a vendor *is* the vendor claim, so ``clean`` overwrote
    # whatever the level said. A submitter with a Dell membership therefore saw Dell
    # preselected next to a live tier dropdown offering Community and AlmaLinux, and
    # picking either changed nothing. Removing vendor from the tier list (which happened
    # earlier) fixed the two answers *contradicting* each other but left one of them inert.
    #
    # Choices are built in ``_build_attribution_field``; ``clean`` maps the answer back to
    # the ``on_behalf_of`` and ``claimed_validation_level`` pair the rest of the pipeline
    # already speaks, so nothing downstream had to learn a new shape.
    attribution = forms.ChoiceField(
        choices=[], required=False, label="Validating as",
        help_text="Who this run counts as. Choosing a vendor you represent makes it that "
                  "vendor's own certification of its own hardware.",
    )
    submitter_notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), required=False,
        label="Notes for the reviewer",
        help_text="Never published - only reviewers see this. Firmware "
                  "settings, known quirks, anything that helps someone "
                  "assessing the run.",
    )

    # The embargo, reviewable before submitting.
    #
    # Both values arrive at ingest, from the CLI's run metadata or the web upload form, and
    # until now the submitter could only *see* the result as a line on the run page:
    # "Embargoed until 2026-10-01". If the flag was missed on the command line, or the date was
    # typed wrong, or a machine stopped being unreleased between the run and the submission,
    # there was nowhere to fix it - and the consequence of each mistake is the wrong thing
    # happening publicly at approval.
    #
    # On this form rather than a separate control because this is the page somebody reviews
    # before handing the run to a reviewer, which is the last moment either value can still be
    # changed without a reviewer's help.
    pre_release = forms.BooleanField(
        required=False, label="Unreleased hardware",
        help_text="Withhold everything this run certifies from public view until the date "
                  "below. Approving an embargoed run records the decision and nothing "
                  "appears in the catalog until then.",
    )
    publish_requested_date = forms.DateField(
        required=False, label="Publish on or after",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Leave blank to keep it withheld until a reviewer or an administrator "
                  "releases it by hand.",
    )
    # Offered only for a run on AlmaLinux Kitten. ``_build_gate_fields`` removes it otherwise,
    # and deliberately: a pass on a shipped release has nothing to wait for, so asking would
    # invite a disclaimer onto a claim that is already true.
    available_from_minor = forms.IntegerField(
        required=False, min_value=0, label="Support starts in minor",
        widget=forms.NumberInput(attrs={"min": "0", "placeholder": "e.g. 3"}),
        help_text="The minor this hardware's enablement lands in. The listing is published "
                  "either way and says so until that minor ships.",
    )


    def __init__(self, *args, subject: str = "system", run=None, user=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.subject = subject
        self.run = run
        self.user = user
        copy = self.SUBJECT_FIELD.get(subject, self.SUBJECT_FIELD["system"])
        self.fields["name"].label = copy["label"]
        self.fields["name"].help_text = copy["help"]
        self.fields["model_number"].help_text = copy["model_help"]
        # Always offered, never removed: the detected kind is a heuristic over
        # firmware strings and is wrong often enough that the submitter - who
        # can see the machine - has to be able to overrule it. Only mandatory
        # when nothing was detected, where there is no prefill to fall back on.
        if subject == "machine":
            self.fields["machine_kind"].required = True

        # Known values for the free-text fields. Scoped to what each field
        # means: the name list is the listings a submitter might be about to
        # duplicate, which for a custom build is motherboards rather than
        # systems.
        self.fields["vendor_name"].widget = ComboBoxInput(self._vendor_names())
        self.fields["name"].widget = ComboBoxInput(self._listing_names(subject))
        self.fields["model_number"].widget = ComboBoxInput(self._model_numbers())
        self.fields["cpu_model"].widget = ComboBoxInput(self._cpu_model_names())

        self.fields["cpu_family"].choices = self._family_choices()
        # A detected model makes the family a derived value, so offering the
        # picker would invite a contradiction between the two.
        if run is not None and (run.cpu_model or "").strip():
            del self.fields["cpu_family"]

        self._build_attribution_field(user, run)
        self._drop_maintenance_fields()
        self._build_gate_fields()
        self._lock_identity()
        self._build_component_rows()
        self._build_component_fields()

        self._build_release_fields()
        self._build_category_fields(subject)

        # The fourth copy of this rule lived here. ``core.forms.bootstrapify`` is the
        # one implementation; two comments in the tree claimed that was already true.
        bootstrapify(self)

    # The wire format lives in ``results.proposal_keys``; these are aliases so the
    # existing ``self.RELEASE_PREFIX`` call sites keep reading naturally.
    RELEASE_PREFIX = proposal_keys.RELEASE_PREFIX
    CATEGORY_PREFIX = proposal_keys.CATEGORY_PREFIX
    PROPOSE_PREFIX = proposal_keys.PROPOSE_PREFIX

    def _build_attribution_field(self, user, run) -> None:
        """Offer community, AlmaLinux where applicable, and each vendor they may act for.

        One list because they are one question. What used to be a tier dropdown beside a
        vendor dropdown was really "who is validating this", asked twice, where one answer
        silently overrode the other.

        Order is deliberate: yourself first as the default and the commonest case, then the
        Foundation, then vendors. The vendor whose hardware this actually is gets
        preselected by ``vendor_to_attribute``, which already required a submit-role
        membership *and* a verified vendor *and* the hardware resolving to that same vendor
        - a Dell employee validating a Supermicro box is not Dell validating it.

        Dropped entirely when only one entry survives, the same rule the tier dropdown
        followed: a menu of one is not a choice, and rendering it implies otherwise.
        """
        from lumina.results.services import identity_vendors
        from lumina.vendors.services import selectable_levels, vendors_for_submission

        choices = [(ValidationLevel.COMMUNITY, "Myself - community validation")]
        if ValidationLevel.ALMALINUX in selectable_levels(user):
            choices.append(
                (ValidationLevel.ALMALINUX, "AlmaLinux - Certification SIG")
            )
        vendors = list(vendors_for_submission(user).order_by("name")) if user else []
        # This control is about the **machine**, so only its makers belong in it.
        #
        # It briefly offered component vendors too, on the reasoning that ``effective_level``
        # caps the tier per listing, so an Intel-attributed run could not certify a Dell
        # chassis anyway. The data model was right and the control was a lie: a field labelled
        # "Validating as" at the top of a form about a Dell system, offering
        # "Intel - vendor certification", reads as Intel validating the Dell system. That is
        # the exact nonsense the narrowing was introduced to remove.
        #
        # A component vendor's claim is made *on the component*, in
        # ``_build_component_fields``, where the control can say which part it is about.
        #
        # Filtered only when the machine's maker is identifiable. The set is empty for a
        # manufacturer string the catalog has never seen, which is the brand-new vendor
        # submitting their first machine, and restricting on that guess would lock them out of
        # attributing their own hardware to themselves.
        if run is not None and vendors:
            relevant = {vendor.pk for vendor in identity_vendors(run)}
            if relevant:
                vendors = [vendor for vendor in vendors if vendor.pk in relevant]
        for vendor in vendors:
            choices.append((
                f"{self.VENDOR_CHOICE_PREFIX}{vendor.slug}",
                f"{vendor.name} - vendor certification",
            ))

        if len(choices) <= 1:
            del self.fields["attribution"]
            return

        self.fields["attribution"].choices = choices
        if run is not None:
            preselect = self.vendor_to_attribute(run)
            if preselect is not None:
                self.initial.setdefault(
                    "attribution",
                    f"{self.VENDOR_CHOICE_PREFIX}{preselect.slug}",
                )
            elif run.on_behalf_of_id:
                self.initial.setdefault(
                    "attribution",
                    f"{self.VENDOR_CHOICE_PREFIX}{run.on_behalf_of.slug}",
                )
            elif run.claimed_validation_level:
                self.initial.setdefault(
                    "attribution", run.claimed_validation_level,
                )

    def _drop_maintenance_fields(self) -> None:
        """Hide ``description`` and ``vendor_spec_url`` from anyone who is not maintaining
        this listing.

        Two situations, and the rule differs because the question differs:

        - **Creating a listing.** Everything on the form is a new fact about a machine the
          catalog has never seen, description included, so it stays.
        - **Re-validating one that exists.** Only somebody who speaks for the hardware's
          vendor may restate what it is. For anyone else these were never even applied -
          they are written at ``System.objects.create`` time and nowhere else - so a
          community submitter typed prose that was silently discarded.

        Dropped outright at first, on a literal reading of "not things like the
        description". That was wrong in the case that matters most: it left the vendor with
        nowhere to describe their own hardware, and pushed them into a second review round
        through ``hardware:propose_edit`` after the listing had already published.
        """
        from lumina.results.services import existing_listing_for
        from lumina.vendors.services import represents_listing_vendor

        if self.run is None:
            return
        listing = existing_listing_for(self.run)
        if listing is None:
            return                                  # creating it; every field applies
        if represents_listing_vendor(self.user, listing):
            return                                  # their hardware, their description
        for name in self.MAINTENANCE_FIELDS:
            self.fields.pop(name, None)

    def _build_release_fields(self) -> None:
        """One checkbox per supported AlmaLinux release.

        The release this run was performed on is evidence and is recorded
        automatically by ``record_compatibility`` whatever is ticked here.
        These boxes are for the rest: a vendor stating their machine also
        supports 8 when the run happened on 10.

        A minimum-minor dropdown used to sit beside each box. Hardware certifies per major
        now, so there is nothing for it to say - and with it goes the subtlest rule on this
        form, where a locked checkbox posted nothing while its enabled minor cleaned to 0
        and silently widened the claim to "all of 8.x".
        """
        from lumina.releases.models import AlmaLinuxRelease

        # Which majors this machine already claims. Derived here rather than taken from
        # the view's ``initial`` so the lock below and the prefill cannot disagree: a box
        # that arrives ticked but editable is the exact thing this is meant to prevent.
        already_claimed: set[int] = set()
        if self.run is not None:
            from lumina.results.services import claimed_release_ticks

            # Two sources, and both are needed. ``claimed_release_ticks`` covers the
            # catalog listing and the submitter's *other* runs of this machine, but it
            # deliberately excludes this run - it exists to prefill a fresh upload. So
            # editing a run's own saved answer would have found nothing claimed and left
            # every box editable, which is precisely the form being re-opened.
            sources = [claimed_release_ticks(self.run), self.run.listing_proposal or {}]
            already_claimed = {
                proposal_keys.release_major(key)
                for source in sources
                for key, value in source.items()
                if proposal_keys.is_release_key(key) and value
            }

        self._release_fields: list = []
        for release in AlmaLinuxRelease.objects.supported():
            support = f"{self.RELEASE_PREFIX}{release.major}"
            self.fields[support] = forms.BooleanField(
                required=False, label=str(release)
            )
            # A release already claimed for this machine is locked on. Support is only
            # ever *added* here - ``merge_listing_proposal`` unions the majors, so
            # unticking a box has never actually retracted anything - and a checkbox the
            # reader can clear to no effect is worse than one they cannot clear: it
            # invites them to retract a claim, appears to accept it, and then quietly
            # keeps it. Withdrawing support is a reviewer's decision.
            #
            # A disabled checkbox posts nothing, which is exactly right: the merge
            # carries the claim forward from ``previous``.
            if release.major in already_claimed:
                self.fields[support].disabled = True
                self.fields[support].help_text = (
                    "Already claimed for this machine. Support can be added here but "
                    "not withdrawn - ask a reviewer if this is wrong."
                )
            self._release_fields.append((release, support))

    def _build_gate_fields(self) -> None:
        """Ask about the minor only when the run was on AlmaLinux Kitten.

        Reported as the rule: "we should only prompt this from the user side if the run was on
        almalinux kitten. If it's already on a stable release and the tests pass, there's
        nothing to gate as we just proved it works."

        So the control is absent rather than blank for the ordinary case. A blank box invites an
        answer, and any answer here would put a disclaimer on a claim that a shipped release has
        already proved - the opposite of what the field is for.

        A reviewer gets it regardless, on their own form: the submitter may not have set it, or
        may have set it wrongly, and by then the run is no longer theirs to edit.
        """
        if self.run is None or not self.run.ran_on_prerelease_os:
            self.fields.pop("available_from_minor", None)
            return

        # Say what has already shipped, and do not guess the answer.
        #
        # Prefilling "the next minor" would be a guess in a box whose value puts a disclaimer on
        # a public listing - the same trap as prefilling identity fields over an existing catalog
        # entry, where a plausible default invites being accepted unread. The submitter knows
        # which minor carries their patch; what they may not know is where the release stream has
        # got to, so that is what the form tells them.
        release = self.run.alma_release
        if release is not None:
            self.fields["available_from_minor"].help_text += (
                f" The newest shipped minor of AlmaLinux {release.major} is "
                f"{release.major}.{release.latest_minor}."
            )

    def _lock_identity(self) -> None:
        """Keep the fields that say what the machine *is*, but stop them counting.

        They used to be removed outright for anyone who does not speak for the machine's
        vendor. That is why the override could not work as a button: JavaScript cannot reveal
        a field the server never rendered, so the control had to be a second <form> - and the
        only place to put it was inside this one. Browsers ignore a nested <form> and submit
        the outer one, so pressing it saved the proposal with the identity fields absent,
        which blanked them.

        Present-but-locked instead. ``clean`` drops their values unless the submitter has
        explicitly disowned the catalog match, so what reaches ``listing_proposal`` is the
        same as when they were absent, and the template can collapse them behind the
        override.

        Not required while locked: the collapsed block is empty on a form the submitter is
        filling in for the components alone, and a required field nobody can see is an
        unfixable error. ``clean`` asks for a name only on the path that needs one.

        ``existing_listing_for`` already returns None for a disputed run, so this unlocks by
        itself once the override is saved - the fields come back as a normal card, and the
        submitter is describing new hardware.
        """
        from lumina.results.services import existing_listing_for
        from lumina.vendors.services import represents_listing_vendor

        self.identity_locked = False
        if self.run is None:
            return
        listing = existing_listing_for(self.run)
        if listing is None or represents_listing_vendor(self.user, listing):
            return
        self.identity_locked = True
        for name in self.IDENTITY_FIELDS:
            if name in self.fields:
                self.fields[name].required = False

    # --- grouped access for the template -----------------------------------

    IDENTITY_FIELDS = ("vendor_name", "name", "machine_kind", "model_number",
                       "description", "vendor_spec_url")
    # The two that only the hardware's vendor may set on an existing listing.
    MAINTENANCE_FIELDS = ("description", "vendor_spec_url")
    CPU_FIELDS = ("cpu_model", "cpu_family")
    ATTRIBUTION_FIELDS = ("attribution",)
    # One card, two gates. They hold a claim back for unrelated reasons - secrecy and timing -
    # and they compose, so a Kitten run on unreleased hardware is gated twice and each lifts on
    # its own schedule. Grouped because the question a submitter is answering is the same one:
    # "is there any reason this should not be public yet?"
    EMBARGO_FIELDS = ("pre_release", "publish_requested_date", "available_from_minor")

    def _present(self, names):
        return [self[name] for name in names if name in self.fields]

    @property
    def identity_rows(self):
        return self._present(self.IDENTITY_FIELDS)

    @property
    def cpu_rows(self):
        return self._present(self.CPU_FIELDS)

    @property
    def attribution_rows(self):
        return self._present(self.ATTRIBUTION_FIELDS)

    @property
    def embargo_rows(self):
        return self._present(self.EMBARGO_FIELDS)

    @property
    def release_rows(self):
        """(release, supported-checkbox) per supported major."""
        return [
            (release, self[support]) for release, support in self._release_fields
        ]

    @property
    def category_rows(self):
        return [
            (category, self[field], self[propose] if propose else None)
            for category, field, propose in self._category_fields
        ]

    def _build_category_fields(self, subject: str) -> None:
        """Taxonomy pickers, restricted to categories that apply here.

        A custom build is listed as a component, so component-only axes appear
        and system-only ones do not.
        """
        from lumina.taxonomy.forms import category_picker_field
        from lumina.taxonomy.models import Category, CategoryValue

        applies = (
            Category.APPLIES_COMPONENT if subject == "motherboard"
            else Category.APPLIES_SYSTEM
        )
        self._category_fields: list = []
        categories = (
            Category.objects.filter(
                applies_to__in=[applies, Category.APPLIES_BOTH]
            )
            # Derived facets are bound from the run itself at approval, so the
            # submitter is neither asked nor able to disagree.
            .exclude(derived_from_runs=True)
            .prefetch_related("values")
            .order_by("display_order", "name")
        )

        for category in categories:
            approved = [
                (value.slug, value.value)
                for value in category.values.all()
                if value.status == CategoryValue.STATUS_APPROVED
            ]
            if not approved:
                continue
            field_name = f"{self.CATEGORY_PREFIX}{category.slug}"
            self.fields[field_name] = category_picker_field(category, approved)

            propose_name = None
            if category.allow_suggestions:
                propose_name = f"{self.PROPOSE_PREFIX}{category.slug}"
                self.fields[propose_name] = forms.CharField(
                    required=False, label=f"Propose new {category.name.lower()}",
                    help_text="Reviewed before it becomes a filter option.",
                )
            self._category_fields.append((category, field_name, propose_name))
    @staticmethod
    def _listing_names(subject: str) -> list:
        """Existing listings of the kind this form is describing.

        A custom build is listed as a motherboard, so offering System names
        there would invite the submitter to name their board after a server.
        """
        from lumina.hardware.models import ComponentKind

        if subject == "motherboard":
            queryset = Component.objects.filter(
                kind=ComponentKind.motherboard.value
            )
        else:
            queryset = System.objects.all()
        return list(queryset.order_by("name").values_list("name", flat=True))

    @staticmethod
    def _model_numbers() -> list:
        numbers = set(
            System.objects.exclude(model_number="")
            .values_list("model_number", flat=True)
        )
        numbers |= set(
            Component.objects.exclude(model_number="")
            .values_list("model_number", flat=True)
        )
        return sorted(numbers)

    @staticmethod
    def _cpu_model_names() -> list:
        """Specific CPU parts only.

        Families are excluded deliberately: this field records the exact model,
        and typing a family name here would log a family as though it were a
        part, which is what the separate picker is for.
        """
        from lumina.hardware.models import ComponentKind, ComponentRole

        return list(
            Component.objects.filter(
                kind=ComponentKind.cpu.value, role=ComponentRole.MODEL
            ).order_by("name").values_list("name", flat=True)
        )

    @staticmethod
    def _family_choices() -> list:
        from lumina.hardware.models import Component, ComponentKind, ComponentRole

        families = Component.objects.filter(
            kind=ComponentKind.cpu.value, role=ComponentRole.FAMILY
        ).select_related("vendor").order_by("vendor__name", "name")
        return [("", "- Not known -")] + [
            (str(family.pk), f"{family.vendor.name} - {family.name}")
            for family in families
        ]

    @staticmethod
    def _vendor_choices(user) -> list:
        """Vendors this user may submit for: verified, with a submit role."""
        from lumina.vendors.models import VendorMembership

        if user is None or not getattr(user, "is_authenticated", False):
            return [("", "- None (submitting as yourself) -")]
        memberships = VendorMembership.objects.filter(
            user=user, role__in=VendorMembership.SUBMIT_ROLES, vendor__verified=True,
        ).select_related("vendor").order_by("vendor__name")
        return [("", "- None (submitting as yourself) -")] + [
            (m.vendor.slug, m.vendor.name) for m in memberships
        ]

    def clean(self):
        cleaned = super().clean()
        # Unpack the single attribution answer into the pair the rest of the pipeline
        # speaks: an ``on_behalf_of`` slug and a ``claimed_validation_level``. Done here so
        # the merge into ``listing_proposal``, the audit entry, and ``effective_level`` at
        # approval all keep working unchanged.
        #
        # Naming a vendor *is* the vendor claim, which is why these were never two
        # independent answers. ``effective_level`` re-derives the same thing at approval,
        # capped by what the submitter may actually act for, so nothing here grants trust -
        # a crafted post naming a vendor the submitter does not represent gets the tier it
        # is entitled to, not the one it asked for.
        # What the locked identity fields amount to. Discarded unless the submitter said
        # this is not that machine, so somebody filling the form in for the components alone
        # cannot restate a listing by posting values the page never showed them - the same
        # guarantee the fields' absence used to give, now stated where it can be tested.
        if getattr(self, "identity_locked", False):
            if cleaned.get("identity_disputed"):
                if not (cleaned.get("name") or "").strip():
                    self.add_error("name", "Say what this machine is, so it can be listed "
                                           "on its own.")
            else:
                for name in self.IDENTITY_FIELDS:
                    cleaned.pop(name, None)

        # A publish date only means something for an embargoed run, and silently ignoring one
        # would surprise the submitter at exactly the wrong moment. The same rule
        # ``BundleUploadForm`` applies, stated again here because this form is a second way in
        # and a rule enforced on one path only is not a rule.
        if cleaned.get("publish_requested_date") and not cleaned.get("pre_release"):
            self.add_error(
                "pre_release",
                "Tick this to withhold the results until the publish date.",
            )

        answer = (cleaned.get("attribution") or "").strip()
        if answer.startswith(self.VENDOR_CHOICE_PREFIX):
            cleaned["on_behalf_of"] = answer[len(self.VENDOR_CHOICE_PREFIX):]
            cleaned["claimed_validation_level"] = ValidationLevel.VENDOR
        else:
            cleaned["on_behalf_of"] = ""
            cleaned["claimed_validation_level"] = answer or ValidationLevel.COMMUNITY
        return cleaned

    @classmethod
    def initial_from_run(cls, run) -> dict:
        """Prefill from whatever the run actually identified.

        A custom build is identified by its motherboard, so that is what gets
        prefilled there. An unidentified machine prefills nothing: its firmware
        named no manufacturer, and guessing would put a placeholder in the
        catalog under the submitter's name.
        """
        from lumina.results.models import SystemKind
        from lumina.vendors.services import resolve_vendor

        if run.system_kind == SystemKind.PREBUILT:
            raw_vendor, name = run.system_vendor, run.system_product
            model_number = run.system_model_number
        else:
            # Custom is the fallback kind, so this covers a self-build and a machine whose
            # firmware named nothing at all. The latter prefills blanks, which is the honest
            # answer: there is nothing detected to review.
            raw_vendor, name = run.board_vendor, run.board_model
            model_number = ""

        vendor = resolve_vendor(raw_vendor) if raw_vendor else None
        initial = {
            "vendor_name": vendor.name if vendor else raw_vendor,
            "name": name,
            "model_number": model_number,
            # Always one of the two kinds. It used to be blank for an "unknown" machine so the
            # radio read as unanswered rather than pre-answered with a guess; there is no third
            # kind now, and custom is the fallback rather than a guess - a machine is claimed to
            # be a vendor-built product or it is not.
            "machine_kind": run.system_kind,
            # The collector already knows the exact part; the submitter is
            # reviewing it, not typing it.
            "cpu_model": run.cpu_model or "",
        }
        # No attribution key here: ``_build_attribution_field`` preselects from the run
        # itself, so setting it in two places would let them disagree about which vendor
        # to default to.
        initial["submitter_notes"] = run.submitter_notes or ""
        # Whatever the CLI or the upload form recorded, so the form shows what is actually set
        # rather than an empty pair of controls implying nothing is.
        initial["pre_release"] = run.pre_release
        initial["publish_requested_date"] = run.publish_requested_date
        initial["available_from_minor"] = run.available_from_minor
        initial.update(cls.detected_releases(run))
        return initial

    @classmethod
    def detected_releases(cls, run) -> dict:
        """Tick every major this machine has a run for.

        Aggregated across the submitter's other unsubmitted runs of the same
        machine, not just this one: somebody uploading 8.10, 9.6 and 10.2
        together has evidence for three releases, and making them tick two of
        them by hand on a form that already knows is busywork.

        The minor each run passed on is no longer part of the claim, only of the run's own
        record. A 9.6 pass ticks 9, and the box means the major.
        """
        from lumina.results.services import sibling_draft_runs

        initial: dict = {}
        for member in [run, *sibling_draft_runs(run)]:
            if member.alma_release_id is None:
                continue
            initial[f"{cls.RELEASE_PREFIX}{member.alma_release.major}"] = True
        return initial

    @staticmethod
    def vendor_to_attribute(run):
        """The vendor to preselect as "on behalf of", or None.

        Requires all three: the submitter is a member of the vendor with a
        submit role, the vendor is verified, and the hardware being submitted
        resolves to that same vendor. The last condition is the point - a Dell
        employee validating a Supermicro box is not Dell validating it, and
        preselecting Dell there would quietly overstate the evidence.
        """
        from lumina.results.models import SystemKind
        from lumina.vendors.models import VendorMembership
        from lumina.vendors.services import resolve_vendor

        submitter = run.submitter
        if submitter is None or not submitter.is_authenticated:
            return None
        reported = (
            run.system_vendor
            if run.system_kind == SystemKind.PREBUILT
            else run.board_vendor
        )
        hardware_vendor = resolve_vendor(reported) if reported else None
        if hardware_vendor is None or not hardware_vendor.verified:
            return None
        member = VendorMembership.objects.filter(
            user=submitter, vendor=hardware_vendor,
            role__in=VendorMembership.SUBMIT_ROLES,
        ).exists()
        return hardware_vendor if member else None


class RunComponentTiesForm(ComponentTiesMixin, forms.Form):
    """The reviewer's copy of the component controls: keep or drop, vendor, and model.

    Same three controls the submitter gets, from the other seat. A reviewer sees the whole
    submission and often knows the catalog better - that "OEM" is ASRock, that a GPU string
    is a UHD Graphics 630 - and they are the last person who can fix it before approval
    creates the entries.

    The same mixin rather than a second implementation, because the thing being edited is
    what ``component_tie_targets`` will do, and two editors that disagree about that is the
    drift the whole arrangement exists to prevent.
    """

    def __init__(self, *args, run, **kwargs):
        self.run = run
        super().__init__(*args, **kwargs)
        self._build_component_rows()
        self._build_component_fields()
        bootstrapify(self)


class RunListingAssignForm(forms.Form):
    """Reviewer links a run to catalog listings before approving it.

    A prebuilt machine (Dell R720) links to a System listing. A custom build
    has no vendor system model, so its run links to the Components it
    exercised instead - typically the motherboard and CPU.
    """

    # Searchable, not free text: a reviewer assigning an *existing* listing must
    # land on one that exists, so the field stays a strict choice and only the
    # picking gets easier. combobox.js turns these into filter-as-you-type
    # dropdowns; without it they are ordinary selects that still work.
    system = forms.ModelChoiceField(
        queryset=System.objects.select_related("vendor").order_by(
            "vendor__name", "name"
        ),
        required=False,
        label="System listing",
        help_text="For prebuilt machines. Leave blank for custom builds.",
        widget=forms.Select(attrs={"data-combobox": "true"}),
    )
    components = forms.ModelMultipleChoiceField(
        queryset=Component.objects.select_related("vendor").order_by(
            "kind", "vendor__name", "name"
        ),
        required=False,
        label="Component listings",
        help_text=(
            "The motherboard, CPU, and anything else this run is evidence for. "
            "Search and add them one at a time; already-linked components are "
            "listed with a remove button."
        ),
        # A multi-select hid what was already attached behind a scroll box and
        # made ctrl-click the only way to add a second thing. Rendered as a
        # search-and-add list instead: see _component_picker.html.
        widget=forms.SelectMultiple(attrs={
            "data-picker": "true",
            "data-picker-placeholder": "Search components to add\u2026",
        }),
    )
    claimed_validation_level = forms.ChoiceField(
        choices=ValidationLevel.choices,
        required=False,
        label="Validation level",
        # Asked directly: "the ability to set the validation level is unclear because it is not
        # per-version, so what will setting this value do? Set it for all versions? Only the
        # submitted version? Components? Only the system?"
        #
        # Every one of those is answerable from the code and none of it was on the page. It is a
        # *ceiling* on this run's evidence, not a value written anywhere: approval records one
        # attestation per listing this run attests, each at the tier that listing's own cap
        # allows, and against the ``ListingVersion`` for the release the run passed on. Other
        # releases keep whatever their own evidence earned, and a listing's badge is then the
        # highest across its versions.
        help_text=(
            "A ceiling on what this run's evidence counts as, not a value written to a "
            "listing. Approving records one attestation per listing below - the system and "
            "each attached part - for the AlmaLinux release this run passed on and no other. "
            "Each is capped separately by what the submitter may claim for that listing, so a "
            "vendor tier reaches only the parts that vendor makes."
        ),
    )
    # The reviewer's copy of the embargo. Same two controls the submitter gets, for the same
    # reason the timing gate is here: by the time a reviewer is looking, the run is no longer the
    # submitter's to edit, and a wrong answer here is the difference between unannounced hardware
    # staying secret and appearing in a public catalog.
    #
    # Clearing the tick on an already-approved, still-held run is also the only way a hold with
    # no date ends - there is no date for anything to fire on. ``assign_listing`` publishes it.
    pre_release = forms.BooleanField(
        required=False, label="Unreleased hardware",
        help_text="Withhold everything this run certifies from public view. With no date "
                  "below it stays withheld until somebody clears this box.",
    )
    publish_requested_date = forms.DateField(
        required=False, label="Publish on or after",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="A date that has already passed publishes at once.",
    )

    # The reviewer's copy of the timing gate.
    #
    # Offered whatever the run reported, unlike the submitter's, and that asymmetry is the point:
    # the submitter is only asked when the run was on Kitten because a shipped release has
    # nothing to wait for, but a reviewer is the backstop for a submitter who left it unset or
    # set it wrongly - and by the time they are looking, the run is no longer the submitter's to
    # edit. They can also clear one that should not be there.
    available_from_minor = forms.IntegerField(
        required=False, min_value=0, label="Support starts in minor",
        widget=forms.NumberInput(attrs={"min": "0", "placeholder": "e.g. 3"}),
        help_text="For evidence from AlmaLinux Kitten: the minor the hardware enablement "
                  "lands in. The listing publishes either way and carries a note naming this "
                  "minor until it ships. Blank means nothing to wait for.",
    )
    machine_kind = forms.ChoiceField(
        choices=[("", "- Leave as detected -")] + [
            ("prebuilt", "A vendor-built system, with its own model name"),
            ("custom", "A custom build, identified by its motherboard"),
        ],
        required=False,
        label="Correct the machine kind",
        help_text="Prebuilt systems often fail to identify themselves: a vendor "
                  "that mirrors its system name into the baseboard reads as a "
                  "custom build. Setting this here is remembered against the "
                  "machine's firmware strings, so later runs of it are "
                  "classified correctly without anyone repeating the fix.",
    )

    # The two gates, kept apart from the assignment on the page.
    #
    # They were rendered with everything else on this form, which put them inside the collapsed
    # block labelled "Attest a different listing" - so a reviewer looking for the embargo could
    # not find it, and reasonably wondered whether the absence was deliberate. Reported exactly
    # that way. Withholding a run and re-pointing it at another listing are unrelated decisions;
    # only the second is an override of what the page already says.
    GATE_FIELDS = ("pre_release", "publish_requested_date", "available_from_minor")

    def __init__(self, *args, run=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.run = run
        # A scoped run has no system to assign, so the picker is removed rather than offered and
        # rejected. ``assign_listing`` raises ``ReviewError`` if a System arrives for one, which is
        # the right backstop and a poor interface: the reviewer was shown a dropdown of every
        # machine in the catalog, under a control reading "Attest a different listing", and only
        # found out it was refused by submitting it. Removing the field also removes the field from
        # the bound form, so a hand-crafted POST cannot smuggle one past the page either.
        if run is not None and run.is_scoped:
            del self.fields["system"]
            # Both describe the machine as the subject of a listing, which a scoped run never
            # produces. ``machine_kind`` is read only by ``create_listings_from_run``'s machine
            # branches, which a scoped run does not reach.
            del self.fields["machine_kind"]
            kinds = " and ".join(run.scope_labels)
            self.fields["components"].help_text = (
                f"The {kinds} this run is evidence for. Approving ties and attests these and "
                "nothing else: the host machine is context, and is never certified by a scoped "
                "run."
            )
            self.fields["claimed_validation_level"].help_text = (
                "A ceiling on what this run's evidence counts as, not a value written to a "
                f"listing. Approving records one attestation per {kinds} below, for the AlmaLinux "
                "release this run passed on and no other. Each is capped separately by what the "
                "submitter may claim for that part, so a vendor tier reaches only the parts that "
                "vendor makes."
            )

    @property
    def gate_rows(self):
        """The withhold controls, shown outright."""
        return [self[name] for name in self.GATE_FIELDS if name in self.fields]

    @property
    def assignment_rows(self):
        """Everything else: which listings this run attests, and at what ceiling."""
        return [
            self[name] for name in self.fields if name not in self.GATE_FIELDS
        ]

    @property
    def identity_summary(self) -> str:
        """What approving would create, for the note under the blank form."""
        if self.run is None:
            return "this run's reported hardware"
        # The claim, not the chassis it was measured in. This note is what tells a reviewer what
        # leaving the form blank will attest, so naming the host here was naming the one thing
        # approving a scoped run cannot touch.
        if self.run.is_scoped:
            kinds = " and ".join(self.run.scope_labels)
            return self.run.claim_subject or f"this run's {kinds}"
        proposal = self.run.listing_proposal or {}
        parts = [
            proposal.get("vendor_name") or self.run.system_vendor,
            proposal.get("name") or self.run.system_product,
        ]
        return " ".join(part for part in parts if part) or "this run's hardware"
