"""Which reports this server will read.

The schema is a contract both projects are written against, and it says plainly: "additive
optional fields bump the minor version, breaking changes bump the major version". Lumina checked
an exact allowlist instead, so every additive release of the suite was an outage here - 1.2 added
``summary.fields`` and nothing else, and every submission from a suite carrying it was refused
until somebody edited a Python set and deployed.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from lumina.results import ingest
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("schema-sub", password="pw")


def _ingest(submitter, version):
    return ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            schema_version=version, run_types=["validate"],
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2"])
def test_every_minor_of_this_major_is_read(submitter, version):
    """1.2 is the one that was refused in production."""
    assert _ingest(submitter, version).schema_version == version


def test_a_minor_this_server_has_never_heard_of_is_read(submitter):
    """The whole point. An additive field is one an older reader can ignore, so a suite that adds
    one must not need a deploy here before anybody can submit."""
    assert _ingest(submitter, "1.9").schema_version == "1.9"


def test_a_new_major_is_refused(submitter):
    """A major says something a reader cannot ignore changed."""
    with pytest.raises(ingest.UnsupportedSchema, match="2.0"):
        _ingest(submitter, "2.0")


def test_a_missing_version_is_refused(submitter):
    with pytest.raises(ingest.UnsupportedSchema):
        _ingest(submitter, None)


def test_something_that_is_not_a_version_is_refused(submitter):
    with pytest.raises(ingest.UnsupportedSchema):
        _ingest(submitter, "banana")


def test_a_leading_digit_is_not_enough(submitter):
    """"12.0" starts with a 1 and is not this major."""
    with pytest.raises(ingest.UnsupportedSchema):
        _ingest(submitter, "12.0")


def test_the_message_says_what_is_accepted(submitter):
    with pytest.raises(ingest.UnsupportedSchema, match=r"accepts 1\.x"):
        _ingest(submitter, "3.1")


def test_a_survey_bundle_follows_the_same_rule(submitter):
    """One check, shared: a server that reads a 1.2 run and refuses a 1.2 survey would be two
    answers to one question."""
    from lumina.survey import ingest as survey_ingest

    with pytest.raises(ingest.UnsupportedSchema):
        survey_ingest._validate_survey_report({"schema_version": "2.0"})

    survey_ingest._validate_survey_report({
        "schema_version": "1.2", "inventory": {"summary": {}}, "environment": {},
    })


def test_the_fields_a_new_minor_added_are_read(submitter):
    """Accepting 1.2 is only half of it: what 1.2 carries has to arrive intact, or the naming
    rules written against ``dmi.*`` read nothing on exactly the bundles that have it."""
    from lumina.results import naming

    inventory = f.default_inventory()
    inventory["summary"]["fields"] = {
        "dmi": {"processor": [{"props": {"Part Number": "Q80-30"}}]},
    }
    run = ingest.ingest_bundle(
        submitter=submitter, source="api",
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            schema_version="1.2", run_types=["validate"], inventory=inventory,
            results=[f.validate_result("validate.cpu.functional")],
        ))),
    )

    context = naming.build_context("cpu", {}, run.inventory)
    assert naming.lookup(context, "dmi.processor.*.Part Number") == "Q80-30"
