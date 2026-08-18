"""Survey forms: the request for long-lived-token capability."""
from __future__ import annotations

from django import forms


class SurveyTokenRequestForm(forms.Form):
    justification = forms.CharField(
        label="Why do you need long-lived survey tokens?",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=(
            "Fleet size, what is automated, how often it runs - whatever helps a "
            "reviewer decide."
        ),
    )
