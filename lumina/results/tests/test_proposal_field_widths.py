"""A submitted value must not be wider than the column that will store it.

The report: approving a run returned a 500. ``DataError: (1406, "Data too long for column
'vendor_spec_url' at row 1")``, raised from ``System.objects.create`` inside the approval
transaction, so the whole approval rolled back and the reviewer was stuck on a field they had
never filled in and could not see.

The gap is between two Django defaults that disagree. ``models.URLField`` caps at 200;
``forms.URLField`` caps at nothing. Every other proposal field was declared with a matching
``max_length`` on both sides, so only the URL was open. The value passed the form, went into
``listing_proposal`` as JSON, which has no widths at all, and was read back weeks later at the
one moment where failing costs somebody their whole approval.

Nothing caught it because SQLite does not enforce ``VARCHAR`` length and the suite runs on
SQLite - MariaDB in strict mode does. So these tests assert the *declared* widths rather than
waiting for a database to complain, which is the only form of this test that fails on both.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.core.models import URL_MAX_LENGTH
from lumina.hardware.models import HardwareListing, ListingEditProposal
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db

# Over the limit by one, so the test states the boundary rather than a round number nobody
# can trace back to anything.
TOO_LONG = "https://vendor.example/specs/" + "x" * (URL_MAX_LENGTH + 1)


@pytest.fixture
def submitter():
    return User.objects.create_user("width-sub")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("width-rev")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


def _run(submitter, proposal) -> TestRun:
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run = release(TestRun.objects.get(pk=run.pk))
    run.listing_proposal = proposal
    run.save(update_fields=["listing_proposal"])
    return run


# --- the declared widths line up --------------------------------------------------


def test_both_columns_use_the_stated_limit():
    """One number, not three. The listing and the edit proposal that feeds it must agree, or
    a proposal is accepted and then cannot be applied."""
    for model in (HardwareListing, ListingEditProposal):
        assert model._meta.get_field(
            "vendor_spec_url").max_length == URL_MAX_LENGTH


# Every form with a ``vendor_spec_url`` the user can type into. The three hand-declared ones
# were all unbounded; ``ListingEditProposalForm`` is a ModelForm and inherits the column's
# limit, and is listed so that staying a ModelForm is something a test notices.
SPEC_URL_FORMS = [
    "lumina.results.forms.RunListingProposalForm",
    "lumina.hardware.forms.ReviewerListingEditForm",
    "lumina.hardware.forms.SubmissionForm",
    "lumina.hardware.forms.ListingEditProposalForm",
]


def _spec_url_field(form_path: str):
    import importlib

    module_name, _, class_name = form_path.rpartition(".")
    form_class = getattr(importlib.import_module(module_name), class_name)
    return form_class.base_fields["vendor_spec_url"]


@pytest.mark.parametrize("form_path", SPEC_URL_FORMS)
def test_every_form_that_writes_a_spec_url_bounds_it(form_path):
    """The actual bug: a form field with no ``max_length`` in front of a column that has one.

    Asserted per form rather than once, because each is a separate declaration and the next
    one added will be written by copying whichever of these the author found first.
    """
    declared = _spec_url_field(form_path).max_length

    assert declared == URL_MAX_LENGTH, (
        f"{form_path}.vendor_spec_url accepts {declared!r}, so it takes values its column "
        "rejects"
    )


@pytest.mark.parametrize("form_path", SPEC_URL_FORMS)
def test_an_over_long_url_is_refused_at_the_form(form_path):
    """End of the prevention half: the person who typed it is told, on the page where they
    typed it, instead of a reviewer meeting a 500 weeks later.

    On the field rather than the form, because these four are built with different required
    arguments and what is being checked is the one field they share.
    """
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _spec_url_field(form_path).clean(TOO_LONG)


# --- a proposal already stored cannot break an approval ----------------------------


def test_an_over_long_stored_url_does_not_break_approval(submitter, reviewer):
    """Every proposal already in the database was written before the forms bounded this, and
    approval is where they are read. Bounding the form does nothing for those."""
    run = _run(submitter, {"vendor_name": "Dell Inc.", "name": "Dell G15 5511",
                           "machine_kind": "prebuilt", "vendor_spec_url": TOO_LONG})
    services.approve_run(run, by=reviewer)

    listings = services.create_listings_from_run(run, by=reviewer)

    assert [listing.name for listing in listings] == ["Dell G15 5511"]
    assert listings[0].vendor_spec_url == ""


def test_dropping_it_is_recorded_with_the_value(submitter, reviewer):
    """The column cannot hold it either, so the audit entry is the only place the URL
    survives. Without it the link is gone and nobody can say what it was."""
    from lumina.audit.models import AuditLogEntry

    run = _run(submitter, {"vendor_name": "Dell Inc.", "name": "Dell G15 5511",
                           "machine_kind": "prebuilt", "vendor_spec_url": TOO_LONG})
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)

    entry = AuditLogEntry.objects.filter(action="test_run.proposal_value_dropped").first()
    assert entry is not None
    assert entry.after["field"] == "vendor_spec_url"
    assert entry.after["value"] == TOO_LONG
    assert entry.after["length"] == len(TOO_LONG)


def test_a_url_that_fits_is_left_alone(submitter, reviewer):
    """The clamp must not be a quiet way of losing every spec link. A test that only checked
    the drop would pass with the field discarded outright."""
    url = "https://vendor.example/specs/g15-5511"
    run = _run(submitter, {"vendor_name": "Dell Inc.", "name": "Dell G15 5511",
                           "machine_kind": "prebuilt", "vendor_spec_url": url})
    services.approve_run(run, by=reviewer)

    listings = services.create_listings_from_run(run, by=reviewer)

    assert listings[0].vendor_spec_url == url


def test_a_url_exactly_at_the_limit_is_kept(submitter, reviewer):
    """The boundary, so the comparison cannot drift to ``<`` and silently drop a URL that
    fits perfectly well."""
    url = "https://vendor.example/" + "x" * (URL_MAX_LENGTH - len("https://vendor.example/"))
    assert len(url) == URL_MAX_LENGTH
    run = _run(submitter, {"vendor_name": "Dell Inc.", "name": "Dell G15 5511",
                           "machine_kind": "prebuilt", "vendor_spec_url": url})
    services.approve_run(run, by=reviewer)

    listings = services.create_listings_from_run(run, by=reviewer)

    assert listings[0].vendor_spec_url == url


# --- the same gap anywhere else ---------------------------------------------------


def _project_models():
    from django.apps import apps

    return [model for model in apps.get_models()
            if model.__module__.startswith("lumina.")]


def _project_forms():
    """Every Form and ModelForm this project declares, by walking the apps' form modules."""
    import importlib
    import pkgutil

    from django import forms as djf

    import lumina

    found = []
    for module_info in pkgutil.iter_modules(lumina.__path__, "lumina."):
        try:
            module = importlib.import_module(f"{module_info.name}.forms")
        except ModuleNotFoundError:
            continue
        for name in dir(module):
            obj = getattr(module, name)
            if (isinstance(obj, type) and issubclass(obj, djf.BaseForm)
                    and hasattr(obj, "base_fields")
                    and obj.__module__ == module.__name__):
                found.append((f"{module.__name__}.{name}", obj))
    return found


def test_no_url_column_is_wider_or_narrower_than_the_limit():
    """Every URL column at one width, so a value that fits one fits all of them.

    A proposal row and the listing it applies to disagreeing by even a little means a value
    is accepted at submission and then cannot be written on approval - which is the shape of
    the bug this file exists for, just moved one table along.
    """
    from django.db import models as djm

    wrong = {
        f"{model.__name__}.{field.name}": field.max_length
        for model in _project_models()
        for field in model._meta.get_fields()
        if isinstance(field, djm.URLField) and field.max_length != URL_MAX_LENGTH
    }

    assert not wrong, (
        f"these URL columns are not {URL_MAX_LENGTH} wide: {wrong}. Use URL_MAX_LENGTH, or "
        "widen every URL column together."
    )


def test_no_form_accepts_a_url_wider_than_the_column():
    """The sweep the reported 500 needed.

    ``forms.URLField`` defaults to no ``max_length`` at all, so the gap is invisible in the
    source - the model says ``URLField`` and the form says ``URLField`` and only one of them
    has a limit. Swept rather than listed per form, because the next form to be written will
    have the same default and nobody will think to add it to a list.
    """
    from django import forms as djf

    unbounded = {
        f"{form_path}.{field_name}"
        for form_path, form_class in _project_forms()
        for field_name, field in form_class.base_fields.items()
        if isinstance(field, djf.URLField) and field.max_length != URL_MAX_LENGTH
    }

    assert not unbounded, (
        f"these form fields take URLs their column rejects: {sorted(unbounded)}. "
        "Pass max_length=URL_MAX_LENGTH."
    )


def test_a_custom_build_board_drops_it_too(submitter, reviewer):
    """The other branch that writes a proposal URL onto a fresh listing. Two branches, two
    writes, and only one of them being clamped is how the original bug worked."""
    inventory = f.custom_build_inventory()
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )
    run = release(TestRun.objects.get(pk=run.pk))
    run.listing_proposal = {"vendor_name": "ASRock", "name": "B650M PG Riptide",
                            "machine_kind": "custom", "vendor_spec_url": TOO_LONG}
    run.save(update_fields=["listing_proposal"])
    services.approve_run(run, by=reviewer)

    listings = services.create_listings_from_run(run, by=reviewer)

    assert [listing.name for listing in listings] == ["B650M PG Riptide"]
    assert listings[0].vendor_spec_url == ""
