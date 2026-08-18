"""Form fields derived from the taxonomy, shared by the forms that offer categories.

The picker shape is an admin-editable property of the ``Category`` row
(``picker_widget``, editable from the changelist), so which widget a category renders
as is data rather than code. Two forms mapped that data to a field with identical
three-branch logic - hardware's submit form and the run-proposal form - down to the
same ``- Select {name} -`` blank label and the same ``size: "6"`` on the multi-select.
"""
from __future__ import annotations

from django import forms

from lumina.taxonomy.models import Category, PickerWidget


def category_picker_field(
    category: Category, approved: list[tuple[str, str]]
) -> forms.Field:
    """The field a category's ``picker_widget`` asks for.

    ``dropdown`` gets a blank option prepended so it can express "not specified" -
    a required-looking select with no empty choice would silently submit its first
    option. It is also the only single-value shape, which is what
    ``Category.picker_widget``'s "at most one value" note refers to.

    Anything unrecognised falls through to the checkbox grid rather than raising: the
    column is admin-editable, and a category rendering as checkboxes is a far better
    outcome than a form that 500s because someone typed a new value.
    """
    widget = PickerWidget(category.picker_widget)
    label = category.name
    if widget is PickerWidget.dropdown:
        return forms.ChoiceField(
            choices=[("", f"- Select {label.lower()} -")] + approved,
            required=False, label=label,
        )
    if widget is PickerWidget.multiselect:
        return forms.MultipleChoiceField(
            choices=approved, required=False, label=label,
            widget=forms.SelectMultiple(attrs={"size": "6"}),
        )
    return forms.MultipleChoiceField(
        choices=approved, required=False, label=label,
        widget=forms.CheckboxSelectMultiple,
    )


def category_propose_field(category: Category) -> forms.CharField:
    """The free-text "propose a new value" companion to a picker.

    Only meaningful where ``category.allow_suggestions`` is set; callers check that.
    The placeholder is hardware's, which is the more helpful of the two forms' - the
    run-proposal form offered no hint at all.
    """
    noun = category.name.lower()
    return forms.CharField(
        required=False, label=f"Propose new {noun}",
        widget=forms.TextInput(attrs={"placeholder": f"e.g. new {noun}"}),
    )
