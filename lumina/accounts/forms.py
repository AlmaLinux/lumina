"""Accounts forms: device activation and API token creation."""
from __future__ import annotations

from django import forms
from django.conf import settings

from lumina.accounts.models import (
    USER_CODE_ALPHABET,
    AccountSettings,
    ApiToken,
    DeviceAuthRequest,
)

_TTL_CHOICES = [
    (60 * 60 * 4, "4 hours"),
    (60 * 60 * 24, "1 day"),
    (60 * 60 * 24 * 7, "7 days"),
    (60 * 60 * 24 * 30, "30 days"),
]

# Offered only to accounts a reviewer has granted long-lived survey tokens; each
# is included only if it fits the account's cap. See lumina.survey.services.
_LONG_TTL_CHOICES = [
    (60 * 60 * 24 * 90, "90 days"),
    (60 * 60 * 24 * 180, "180 days"),
    (60 * 60 * 24 * 366, "1 year"),
]


def _token_ttl_cap(user) -> int:
    if user is None or not getattr(user, "is_authenticated", False):
        return settings.LUMINA_API_TOKEN_TTL_SECONDS
    from lumina.survey.services import user_token_cap

    return user_token_cap(user)


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

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        cap = _token_ttl_cap(user)
        if cap > settings.LUMINA_API_TOKEN_TTL_SECONDS:
            # A survey-token grant lifts the 30-day ceiling; offer the longer
            # options that fit this account's cap.
            self.fields["ttl_seconds"].choices = _TTL_CHOICES + [
                choice for choice in _LONG_TTL_CHOICES if choice[0] <= cap
            ]

    def clean_ttl_seconds(self) -> int:
        return min(self.cleaned_data["ttl_seconds"], _token_ttl_cap(self.user))


class AccountSettingsForm(forms.ModelForm):
    """Account-wide publishing preferences."""

    # Not a model field: the preference is the default for *new* runs, so turning it on
    # says nothing by itself about the runs already submitted. Offered as a separate,
    # unticked act rather than folded into the checkbox above, because "all my runs" and
    # "my runs from now on" are different requests and a switch that silently rewrote
    # the attribution of already-published work would be the wrong default either way.
    apply_to_existing = forms.BooleanField(
        required=False,
        label="Apply this to my existing runs as well",
        help_text="Updates every run you have already submitted to match, including "
                  "published ones. Leave unticked to change only future runs.",
    )

    class Meta:
        model = AccountSettings
        fields = ["publish_anonymously"]
        labels = {"publish_anonymously": "Publish my runs anonymously"}
        help_texts = {
            "publish_anonymously": (
                "New runs are listed as “Anonymous” instead of your username. Reviewers "
                "and administrators still see who submitted them, and you can attribute "
                "or anonymize any individual run from its own page."
            ),
        }
