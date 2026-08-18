"""Accounts forms: device activation and API token creation."""
from __future__ import annotations

from django import forms
from django.conf import settings

from lumina.accounts.models import USER_CODE_ALPHABET, ApiToken, DeviceAuthRequest

_TTL_CHOICES = [
    (60 * 60 * 4, "4 hours"),
    (60 * 60 * 24, "1 day"),
    (60 * 60 * 24 * 7, "7 days"),
    (60 * 60 * 24 * 30, "30 days"),
]


class ActivateForm(forms.Form):
    user_code = forms.CharField(
        label="Code shown by alma-cert",
        max_length=16,
        widget=forms.TextInput(
            attrs={"placeholder": "BQXK-PMTH", "autocomplete": "off",
                   "autofocus": "autofocus"}
        ),
    )

    def clean_user_code(self) -> str:
        raw = self.cleaned_data["user_code"].strip().upper().replace(" ", "")
        if "-" not in raw and len(raw) == 8:
            raw = f"{raw[:4]}-{raw[4:]}"
        body = raw.replace("-", "")
        if len(body) != 8 or any(c not in USER_CODE_ALPHABET for c in body):
            raise forms.ValidationError("That doesn't look like a valid code.")
        return raw

    def find_request(self) -> DeviceAuthRequest | None:
        return DeviceAuthRequest.objects.pending().filter(
            user_code=self.cleaned_data["user_code"]
        ).first()


class ApiTokenCreateForm(forms.Form):
    name = forms.CharField(label="Token name", max_length=80,
                           help_text="Something to recognize it by later.")
    scopes = forms.MultipleChoiceField(
        choices=ApiToken.SCOPE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        initial=[ApiToken.SCOPE_READ],
    )
    ttl_seconds = forms.TypedChoiceField(
        label="Expires after",
        choices=_TTL_CHOICES,
        coerce=int,
        initial=60 * 60 * 24 * 7,
    )

    def clean_ttl_seconds(self) -> int:
        ttl = self.cleaned_data["ttl_seconds"]
        return min(ttl, settings.LUMINA_API_TOKEN_TTL_SECONDS)
