"""What each platform's webhook body looks like.

One builder per endpoint kind, chosen by ``payloads.for_endpoint``, which is the reason
``kind`` exists on the endpoint: Mattermost gets a rich message, the generic kind keeps
the signed JSON envelope somebody may already be parsing, and a future platform is a new
function rather than another branch in the sender.

Mattermost is sent Slack-compatible attachments rather than ``props.mm_blocks``. Blocks
are richer and interactive, but their buttons need an action endpoint for Mattermost to
call back into, which is a feature with an authorization question attached rather than a
change of payload. Attachments work on every version that has ever had incoming webhooks.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest
from django.contrib.auth.models import User

from lumina.notifications import events, payloads, services
from lumina.notifications.models import WebhookEndpoint
from lumina.results.tests import factories as f

pytestmark = pytest.mark.django_db


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def submitter():
    return User.objects.create_user("pay-sub", email="pay@example.com")


def _run(submitter):
    from lumina.results import ingest

    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(f.make_report())),
        source="api",
    )


def _posted(kind, event_key="run.needs_details"):
    WebhookEndpoint.objects.create(
        name="hook", url="https://hook.invalid/x", kind=kind,
        event_keys=[event_key], secret="s3cret", enabled=True,
    )
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return _FakeResponse()

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        services.deliver_pending()
    return captured


# --- mattermost ------------------------------------------------------------------

def test_a_mattermost_message_is_rich(submitter):
    _run(submitter)

    body = _posted(WebhookEndpoint.KIND_MATTERMOST)["body"]
    attachment = body["attachments"][0]

    assert attachment["title"] == events.EVENTS["run.needs_details"].subject
    assert attachment["title_link"].startswith("https://")
    assert attachment["text"]
    assert attachment["footer"]


def test_the_plain_text_still_carries_the_subject(submitter):
    """It is what a push notification and the channel preview show; an attachment is not
    rendered in either."""
    _run(submitter)

    body = _posted(WebhookEndpoint.KIND_MATTERMOST)["body"]

    assert events.EVENTS["run.needs_details"].subject in body["text"]


def test_the_colour_says_what_kind_of_news_it_is(submitter):
    run = _run(submitter)
    services.emit("run.approved", target=run)

    body = _posted(WebhookEndpoint.KIND_MATTERMOST, event_key="run.approved")["body"]

    assert body["attachments"][0]["color"] == payloads._TONE_COLORS[events.TONE_GOOD]


def test_a_rejection_is_not_the_same_colour_as_an_approval():
    good = payloads._TONE_COLORS[events.TONE_GOOD]
    bad = payloads._TONE_COLORS[events.TONE_BAD]
    attention = payloads._TONE_COLORS[events.TONE_ATTENTION]

    assert len({good, bad, attention}) == 3


def test_the_message_carries_the_machine_it_is_about(submitter):
    _run(submitter)

    body = _posted(WebhookEndpoint.KIND_MATTERMOST)["body"]
    fields = {f["title"]: f["value"] for f in body["attachments"][0]["fields"]}

    assert "Machine" in fields
    assert fields["Submitted by"] == "pay-sub"


def test_an_anonymous_run_is_not_named_in_chat(submitter):
    """A chat channel is a wider audience than the review page, so the published name is
    what goes out."""
    run = _run(submitter)
    run.publish_anonymously = True
    run.save(update_fields=["publish_anonymously"])

    body = _posted(WebhookEndpoint.KIND_MATTERMOST)["body"]
    fields = {f["title"]: f["value"] for f in body["attachments"][0]["fields"]}

    assert fields["Submitted by"] == "Anonymous"
    assert "pay-sub" not in json.dumps(body)


def test_blank_facts_are_left_out_rather_than_shown_empty(submitter):
    _run(submitter)

    body = _posted(WebhookEndpoint.KIND_MATTERMOST)["body"]

    assert all(f["value"] for f in body["attachments"][0]["fields"])


# --- generic ---------------------------------------------------------------------

def test_the_generic_envelope_keeps_its_shape_and_signature(submitter):
    _run(submitter)

    captured = _posted(WebhookEndpoint.KIND_GENERIC)

    assert captured["headers"]["x-lumina-signature"].startswith("sha256=")
    body = captured["body"]
    assert set(body) >= {"event", "id", "created_at", "target", "actor"}
    assert body["target"]["type"] == "testrun"


def test_the_generic_envelope_also_gets_the_facts(submitter):
    _run(submitter)

    body = _posted(WebhookEndpoint.KIND_GENERIC)["body"]

    assert body["fields"]["Machine"]


# --- the registry ----------------------------------------------------------------

def test_every_endpoint_kind_has_a_builder():
    for kind, _label in WebhookEndpoint.KIND_CHOICES:
        assert kind in payloads.BUILDERS, kind


def test_an_unknown_kind_falls_back_to_the_signed_envelope():
    assert payloads.for_endpoint("carrier-pigeon") is payloads.generic


def test_a_target_with_no_facts_simply_has_none():
    assert payloads.target_fields(object()) == []


def test_a_broken_facts_helper_does_not_break_the_delivery():
    class Exploding:
        def notification_fields(self):
            raise RuntimeError("no")

    assert payloads.target_fields(Exploding()) == []


# --- the endpoint form has to expose the whole model -----------------------------


def test_the_endpoint_form_exposes_every_editable_field():
    """``Meta.fields`` is an explicit list, so a field added to the model is invisible in
    the admin until somebody remembers to name it. ``direct_messages`` was added and
    missed exactly that way, and the direct-message feature was unconfigurable while
    looking finished."""
    from lumina.notifications.admin import WebhookEndpointForm

    editable = {
        field.name for field in WebhookEndpoint._meta.get_fields()
        if getattr(field, "editable", False) and not field.auto_created
    }
    # Generated on save and shown read-only in the admin, never typed.
    derived = {"last_status", "last_delivery_at"}

    assert editable - derived <= set(WebhookEndpointForm.Meta.fields), (
        editable - derived - set(WebhookEndpointForm.Meta.fields)
    )


def test_the_direct_message_checkbox_is_actually_rendered():
    from lumina.notifications.admin import WebhookEndpointForm

    rendered = WebhookEndpointForm().as_p()

    assert 'name="direct_messages"' in rendered
    assert "NOT locked to a single channel" in (
        WebhookEndpointForm().fields["direct_messages"].help_text
    )
