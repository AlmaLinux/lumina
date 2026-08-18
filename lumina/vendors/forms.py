"""Forms for vendor profile proposals.

Two ModelForms back the two flows. They both write a VendorProposal - the
view chooses which form to render based on whether the user is creating
(/vendors/propose-new/) or editing an existing vendor
(/vendors/<slug>/propose-edit/).
"""
from __future__ import annotations

from django import forms

from lumina.core.forms import bootstrapify
from lumina.vendors.models import Vendor, VendorClaim, VendorProposal


class VendorCreateProposalForm(forms.ModelForm):
    # Not a VendorProposal field: it chooses the *flow*, not a stored value. Ticked, the view opens
    # a VendorClaim instead of a plain proposal (see vendors.views.propose_new). ``reveal-toggle``
    # folds the claim inputs away until it is ticked, the same CSS-only disclosure the rest of the
    # site uses.
    claim_ownership = forms.BooleanField(
        required=False,
        label="I represent this vendor and want to claim ownership",
        help_text="Tick this only if you work for this vendor. A reviewer will verify it; once "
                  "approved you own the listing and can self-certify. Leave it unticked to just add "
                  "the vendor to the catalog for anyone to use.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input reveal-toggle"}),
    )

    class Meta:
        model = VendorProposal
        fields = ("name", "scope", "homepage", "contact_email", "description", "logo")
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        bootstrapify(self)

    def clean_name(self) -> str:
        name = self.cleaned_data["name"].strip()
        if Vendor.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError(
                f"A vendor named {name!r} already exists. Use the existing one."
            )
        return name


class VendorEditProposalForm(forms.ModelForm):
    """Pre-filled with the current vendor values; submitter overrides only
    what they want changed. Blank fields are treated as "no change" on
    approval (see VendorProposal._apply_update)."""

    class Meta:
        model = VendorProposal
        fields = ("name", "homepage", "contact_email", "description", "logo")
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, vendor: Vendor, **kwargs):
        kwargs.setdefault("initial", {}).update(
            name=vendor.name,
            homepage=vendor.homepage,
            contact_email=vendor.contact_email,
            description=vendor.description,
        )
        super().__init__(*args, **kwargs)
        # Logo is intentionally NOT pre-filled: the file input can't be
        # primed with an existing file, and "leave blank to keep current"
        # is the right UX. The form's helper text spells this out.
        bootstrapify(self)


class VendorClaimForm(forms.ModelForm):
    """The stated case for representing a vendor.

    Deliberately short. A reviewer decides on judgement, so the form asks for
    what a human needs to make that call - an address at the company's domain, a
    role, and a sentence - rather than implementing an automated proof that would
    fail for the engineer who cannot edit their own company's DNS.
    """

    class Meta:
        model = VendorClaim
        fields = ("work_email", "role_at_vendor", "note", "evidence")
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}
        labels = {
            "work_email": "Your work email",
            "role_at_vendor": "Your role there",
            "note": "Anything else that helps us verify this",
            "evidence": "Supporting file (optional)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        bootstrapify(self)
