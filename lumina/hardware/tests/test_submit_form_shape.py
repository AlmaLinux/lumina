"""Submit form UX tests.

These pin down the form shape the template depends on - proper choice
querysets for vendor/CPU pickers, the per-category value fields, and the
"propose new" sidecar inputs. Behavior tests for persistence live in
test_submit_flow.py.
"""
from __future__ import annotations

import pytest
from django import forms
from django.contrib.auth import get_user_model
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.core.forms import bootstrapify
from lumina.hardware.forms import SubmissionForm
from lumina.hardware.models import Component, ComponentKind
from lumina.taxonomy.models import Category, CategoryValue
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def user():
    return User.objects.create_user(username="u")


def test_bootstrapify_picks_a_class_per_widget_kind():
    """The shared widget-class rule, which every submit form depends on.

    ``CheckboxSelectMultiple`` is the one that bites: it descends from
    ``ChoiceWidget``, so it is neither a ``CheckboxInput`` nor a ``Select``. A
    branch naming only those two drops it into the text-input fallback and every
    option renders as a full-width pill instead of a checkbox.

    This rule existed in three copies (hardware, software, vendors) before
    ``lumina.core.forms``, and only hardware's copy handled the checkbox grid.
    """
    class Sample(forms.Form):
        text = forms.CharField()
        flag = forms.BooleanField()
        pick = forms.ChoiceField(choices=[("a", "A")])
        grid = forms.MultipleChoiceField(
            choices=[("a", "A")], widget=forms.CheckboxSelectMultiple,
        )

    form = Sample()
    bootstrapify(form)

    classes = {name: f.widget.attrs["class"] for name, f in form.fields.items()}
    assert classes == {
        "text": "form-control",
        "flag": "form-check-input",
        "pick": "form-select",
        "grid": "form-check-input",
    }


def test_a_software_only_category_is_not_offered_on_a_hardware_submission(
    user, arch_cat
):
    """Adding ``APPLIES_SOFTWARE`` turned a harmless omission into a real bug.

    This form took every ``Category`` with no scope filter, which was correct
    while every category applied to hardware. With a software scope in the table
    it offered a server submitter Backup and Creative to tag a machine with.
    ``results/forms.py`` and ``software/forms.py`` both filter; this one did not.
    """
    software_only = Category.objects.create(
        name="Category", slug="software-category",
        applies_to=Category.APPLIES_SOFTWARE,
    )
    CategoryValue.objects.create(category=software_only, value="Backup")

    form = SubmissionForm(user=user)

    assert "cat_software-category" not in form.fields
    # Not over-broad: a hardware category is still offered.
    assert "cat_architecture" in form.fields


@pytest.fixture
def intel():
    return Vendor.objects.get_or_create(name="Intel")[0]


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell", verified=True)


@pytest.fixture
def arch_cat():
    return Category.objects.create(name="Architecture", slug="architecture")


class VendorChoicesTests:
    def test_vendor_choices_are_existing_vendors(self, user, dell, intel):
        form = SubmissionForm(user=user)
        slugs = {slug for slug, _ in form.fields["vendor"].choices if slug}
        assert "dell" in slugs and "intel" in slugs


class CategoryMultiSelectTests:
    def test_per_category_field_exposes_approved_values(self, user, arch_cat):
        CategoryValue.objects.create(category=arch_cat, value="x86_64")
        CategoryValue.objects.create(category=arch_cat, value="aarch64")
        # A pending value must not appear in the picker.
        CategoryValue.propose(category=arch_cat, value="riscv64", proposed_by=user)

        form = SubmissionForm(user=user)
        field = form.fields["cat_architecture"]
        values = {v for v, _ in field.choices if v}
        assert "x86_64" in values and "aarch64" in values
        assert "riscv64" not in values

    def test_submission_persists_picked_category_values(self, user, dell, arch_cat):
        x86 = CategoryValue.objects.create(category=arch_cat, value="x86_64")
        data = {
            "kind": "system",
            "name": "Test System",
            "model_number": "T1",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "cat_architecture": [x86.slug],
        }
        form = SubmissionForm(data=data, user=user)
        assert form.is_valid(), form.errors
        submission = form.save()
        listing = submission.listing
        assert listing.category_values.filter(value=x86).exists()


class PickerWidgetTests:
    """Each category picks its own widget on the submit form.

    - ``dropdown`` is single-select; structurally prevents a system from
      claiming multiple architectures.
    - ``checkboxes`` and ``multiselect`` both allow multiple values; the
      difference is purely visual (checkbox grid vs scrollable list).
    """

    def test_dropdown_widget_uses_choice_field(self, user, arch_cat):
        from django.forms import ChoiceField, MultipleChoiceField

        from lumina.taxonomy.models import PickerWidget

        arch_cat.picker_widget = PickerWidget.dropdown
        arch_cat.save()
        CategoryValue.objects.create(category=arch_cat, value="x86_64")
        CategoryValue.objects.create(category=arch_cat, value="aarch64")
        form = SubmissionForm(user=user)
        field = form.fields["cat_architecture"]
        assert isinstance(field, ChoiceField)
        assert not isinstance(field, MultipleChoiceField)

    def test_multiselect_widget_uses_multi_choice_with_listbox(self, user, arch_cat):
        from django.forms import CheckboxSelectMultiple, MultipleChoiceField, SelectMultiple

        from lumina.taxonomy.models import PickerWidget

        arch_cat.picker_widget = PickerWidget.multiselect
        arch_cat.save()
        CategoryValue.objects.create(category=arch_cat, value="x86_64")
        form = SubmissionForm(user=user)
        field = form.fields["cat_architecture"]
        assert isinstance(field, MultipleChoiceField)
        assert isinstance(field.widget, SelectMultiple)
        assert not isinstance(field.widget, CheckboxSelectMultiple)

    def test_checkboxes_widget_uses_checkbox_select_multiple(self, user, arch_cat):
        from django.forms import CheckboxSelectMultiple, MultipleChoiceField

        from lumina.taxonomy.models import PickerWidget

        arch_cat.picker_widget = PickerWidget.checkboxes
        arch_cat.save()
        CategoryValue.objects.create(category=arch_cat, value="x86_64")
        form = SubmissionForm(user=user)
        field = form.fields["cat_architecture"]
        assert isinstance(field, MultipleChoiceField)
        assert isinstance(field.widget, CheckboxSelectMultiple)

    def test_dropdown_persists_one_listing_value(self, user, dell, arch_cat):
        from lumina.taxonomy.models import PickerWidget

        arch_cat.picker_widget = PickerWidget.dropdown
        arch_cat.save()
        x86 = CategoryValue.objects.create(category=arch_cat, value="x86_64")
        CategoryValue.objects.create(category=arch_cat, value="aarch64")
        data = {
            "kind": "system", "name": "T", "model_number": "T1",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "cat_architecture": x86.slug,
        }
        form = SubmissionForm(data=data, user=user)
        assert form.is_valid(), form.errors
        submission = form.save()
        bindings = submission.listing.category_values.filter(value__category=arch_cat)
        assert bindings.count() == 1 and bindings.first().value == x86

    def test_dropdown_blank_means_no_binding(self, user, dell, arch_cat):
        from lumina.taxonomy.models import PickerWidget

        arch_cat.picker_widget = PickerWidget.dropdown
        arch_cat.save()
        CategoryValue.objects.create(category=arch_cat, value="x86_64")
        data = {
            "kind": "system", "name": "T", "model_number": "T1",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "cat_architecture": "",
        }
        form = SubmissionForm(data=data, user=user)
        assert form.is_valid(), form.errors
        form.save()
        assert not arch_cat.values.filter(listing_bindings__isnull=False).exists()


class AllowSuggestionsTests:
    """allow_suggestions=False suppresses the propose-new field for that category.

    Used for axes that must come from a curated list (e.g. AlmaLinux versions
    - only the Foundation can release new ones; users shouldn't be proposing
    "AlmaLinux 11" before it exists).
    """

    def test_default_true_exposes_propose_field(self, user, arch_cat):
        # Default behavior: propose field is present.
        assert "propose_architecture" in SubmissionForm(user=user).fields

    def test_false_omits_propose_field(self, user, arch_cat):
        arch_cat.allow_suggestions = False
        arch_cat.save()
        assert "propose_architecture" not in SubmissionForm(user=user).fields

    def test_false_ignores_posted_propose_value(self, user, dell, arch_cat):
        # Even if a malicious client crafts a propose_<slug> value, the form
        # must not promote it because the field doesn't exist on the form.
        arch_cat.allow_suggestions = False
        arch_cat.save()
        data = {
            "kind": "system", "name": "T", "model_number": "T1",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "propose_architecture": "evil_value",
        }
        form = SubmissionForm(data=data, user=user)
        assert form.is_valid(), form.errors
        form.save()
        assert not CategoryValue.objects.filter(value="evil_value").exists()


class CpuPickerTests:
    def test_cpu_choices_limited_to_cpu_kind(self, user, intel, dell):
        cpu = Component.objects.create(
            name="Xeon", vendor=intel, model_number="X1", kind=ComponentKind.cpu
        )
        nic = Component.objects.create(
            name="NIC", vendor=dell, model_number="N1", kind=ComponentKind.nic
        )
        form = SubmissionForm(user=user)
        # Choices store pks as strings (ChoiceField stringifies them); compare str-to-str.
        pks = {pk for pk, _ in form.fields["cpus"].choices}
        assert str(cpu.pk) in pks
        assert str(nic.pk) not in pks

    def test_system_submission_attaches_cpus(self, user, dell, intel):
        cpu = Component.objects.create(
            name="Xeon", vendor=intel, model_number="X1", kind=ComponentKind.cpu
        )
        data = {
            "kind": "system",
            "name": "System With CPU",
            "model_number": "T2",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "cpus": [str(cpu.pk)],
        }
        form = SubmissionForm(data=data, user=user)
        assert form.is_valid(), form.errors
        sub = form.save()
        assert cpu in sub.listing_system.cpus.all()

    def test_cpus_ignored_when_kind_is_component(self, user, dell, intel):
        cpu = Component.objects.create(
            name="Xeon", vendor=intel, model_number="X1", kind=ComponentKind.cpu
        )
        data = {
            "kind": "component",
            "name": "Some Component",
            "model_number": "C1",
            "vendor": dell.slug,
            "claimed_validation_level": ValidationLevel.COMMUNITY,
            "cpus": [str(cpu.pk)],
        }
        form = SubmissionForm(data=data, user=user)
        assert form.is_valid(), form.errors
        # No crash, no attachment: a Component has no .cpus M2M.
        form.save()


def test_the_categories_card_is_hidden_when_there_is_nothing_to_ask(client, user):
    """Reported as "the categories section is blank", and it was.

    Architecture is the only hardware facet left and it is derived from the run's own kernel
    report, so the form excludes it: asking the submitter invites an answer that contradicts the
    machine. With nothing left to render, the card was a heading, a subtitle promising a taxonomy
    picker, and empty space. Both other templates that render this same card already guarded it.
    """
    from lumina.taxonomy.models import Category

    Category.objects.create(
        name="Architecture", slug="architecture", derived_from_runs=True,
    )
    client.force_login(user)

    body = client.get(reverse("submit:start")).content.decode()

    assert "Tag the listing with existing taxonomy values" not in body


def test_the_card_comes_back_for_a_facet_a_submitter_can_answer(client, user, arch_cat):
    """The guard, not a deletion. An admin adding a hardware facet that is not derived from a run
    should get a picker for it without anybody editing a template."""
    CategoryValue.objects.create(category=arch_cat, value="x86_64")
    client.force_login(user)

    body = client.get(reverse("submit:start")).content.decode()

    assert "Tag the listing with existing taxonomy values" in body
