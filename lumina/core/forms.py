"""Form helpers shared by every app that renders a Bootstrap form.

There is one rule here and it lived in three places before this module existed:
``hardware.forms``, ``software.forms`` and ``vendors.forms`` each carried their own
copy. They drifted, which is how the software submit page ended up rendering its
category checkboxes as full-width text inputs. AGENTS.md prescribes a
``StyledForm``/``StyledModelForm`` pair and ``core/_form_field*.html`` templates
for this, none of which exist in the repo; this helper is the actual convention,
and it is the only implementation - ``results.forms`` carried a fourth copy until it
was folded in here.
"""
from __future__ import annotations

from django import forms

# Widgets whose attrs land on individual <input type="checkbox|radio"> elements
# rather than on one control. ``CheckboxSelectMultiple`` and ``RadioSelect``
# descend from ``ChoiceWidget``, so they are neither ``CheckboxInput`` nor
# ``Select`` - miss them and each option renders as a full-width pill with no
# visible box to click.
_CHECK_WIDGETS = (
    forms.CheckboxInput | forms.CheckboxSelectMultiple | forms.RadioSelect
)


def bootstrapify(form: forms.BaseForm) -> None:
    """Give every field in ``form`` the Bootstrap 5 class its widget needs.

    ``setdefault``, so a field that declared its own class keeps it.
    """
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, _CHECK_WIDGETS):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, forms.Select):
            # SelectMultiple subclasses Select, so this covers both.
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


def narrow_level_field(
    form: forms.BaseForm, user, *, field_name: str = "claimed_validation_level"
) -> None:
    """Restrict a tier dropdown to what ``user`` may actually choose, or remove it.

    Three forms carried identical copies of this: hardware's and software's submit
    forms and the run-proposal form, the last spelling the label lookup differently
    for the same result.

    Two things it deliberately does:

    - **Never offers vendor.** Submitting on behalf of a vendor *is* the vendor claim
      (``vendors.services.resolve_claimed_level`` sets it), so listing it here as well
      asks the same question twice and lets the answers disagree.
    - **Deletes the field when one option is left.** A dropdown of one is not a
      choice, and rendering it implies otherwise.

    The dropdown used to list all three tiers and reject the ineligible ones at clean
    time, which is a menu of options that do not work.
    """
    from lumina.core.certification import ValidationLevel
    from lumina.vendors.services import selectable_levels

    field = form.fields.get(field_name)
    if field is None:
        return
    levels = selectable_levels(user)
    field.choices = [(level, ValidationLevel(level).label) for level in levels]
    if len(levels) <= 1:
        del form.fields[field_name]
