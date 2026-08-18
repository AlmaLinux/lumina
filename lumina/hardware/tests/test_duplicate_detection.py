"""Warning a reviewer that a submission may duplicate an existing listing.

The manual submit form creates listings and can no longer target an existing one,
which is what stopped it rewriting other people's compatibility rows before review.
The cost of that is a fork: two people cataloguing one machine get two listings, the
form has no duplicate check (only vendor *names* are checked, in
``SubmissionForm.clean``), and ``generate_unique_slug`` appends "-2" in silence. A
reviewer is the only thing between that and one server listed twice with half the
evidence each.

The run path never had this problem, because a run carries DMI identity and
``results.services.find_matching_system`` matches on it. A declaration carries only
what somebody typed.

**Half of this file is about false positives**, and that is the point. A banner that
cries duplicate over a genuinely different machine gets dismissed, and then it is
worse than no banner: it launders real duplicates past a reviewer who has stopped
reading it. So the matcher compares equality after normalization, never substrings and
never edit distance.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from lumina.core.certification import ValidationLevel
from lumina.hardware.models import Component, ComponentKind, Submission, System
from lumina.hardware.services import annotate_similar_listings, similar_listings
from lumina.vendors.models import Vendor, VendorAlias

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell Inc.", published=True)


def _system(vendor, name, model_number="", published=True):
    return System.objects.create(
        vendor=vendor, name=name, model_number=model_number, published=published,
    )


# --- what must be caught -------------------------------------------------------


def test_the_same_name_under_the_same_vendor_is_flagged(dell):
    existing = _system(dell, "PowerEdge R750")
    fresh = _system(dell, "PowerEdge R750", published=False)

    assert [o for o, _ in similar_listings(fresh)] == [existing]


def test_punctuation_and_case_do_not_hide_a_duplicate(dell):
    existing = _system(dell, "PowerEdge R750")
    fresh = _system(dell, "poweredge  r-750", published=False)

    found = similar_listings(fresh)

    assert [o for o, _ in found] == [existing]
    assert found[0][1] == "same name"


def test_the_vendor_name_inside_the_product_name_does_not_hide_a_duplicate(dell):
    """The most common way two people describe one listing differently.

    ``normalize_vendor_name`` supplies the tokens to strip, so "Inc." is already
    understood as a corporate suffix rather than part of the product.
    """
    existing = _system(dell, "PowerEdge R750")
    fresh = _system(dell, "Dell PowerEdge R750", published=False)

    assert [o for o, _ in similar_listings(fresh)] == [existing]


def test_a_shared_model_number_is_flagged_even_with_a_different_name(dell):
    existing = _system(dell, "PowerEdge R750", model_number="R750")
    fresh = _system(dell, "Rack Server 2U", model_number="r-750", published=False)

    found = similar_listings(fresh)

    assert [o for o, _ in found] == [existing]
    assert found[0][1] == "same model number"


def test_a_duplicate_under_a_forked_vendor_is_still_found(dell):
    """The worst case, and the one a plain foreign-key comparison misses.

    The submitter proposes "Dell" inline while "Dell Inc." already exists.
    ``SubmissionForm.clean`` only rejects an *exact* name collision, so the inline
    vendor is created and its listing looks unrelated to Dell's by FK.
    """
    existing = _system(dell, "PowerEdge R750")
    forked = Vendor.objects.create(name="Dell", published=False)
    fresh = _system(forked, "PowerEdge R750", published=False)

    assert [o for o, _ in similar_listings(fresh)] == [existing]


def test_an_unpublished_duplicate_is_flagged_too(dell):
    """Two pending submissions for one machine is the case worth catching early, and
    it is invisible on the public catalog by definition."""
    other_pending = _system(dell, "PowerEdge R750", published=False)
    fresh = _system(dell, "PowerEdge R750", published=False)

    assert [o for o, _ in similar_listings(fresh)] == [other_pending]


# --- what must NOT be caught ---------------------------------------------------


def test_a_different_model_in_the_same_family_is_not_a_duplicate(dell):
    """"PowerEdge R750" and "PowerEdge R750xd" are different machines.

    This is why the matcher does not do substrings. A prefix test would flag every
    member of a product family as a duplicate of every other.
    """
    _system(dell, "PowerEdge R750")
    fresh = _system(dell, "PowerEdge R750xd", published=False)

    assert similar_listings(fresh) == []


def test_the_same_name_under_a_genuinely_different_vendor_is_not_a_duplicate():
    """Model numbers repeat across manufacturers, and "R750" is not distinctive."""
    dell = Vendor.objects.create(name="Dell Inc.", published=True)
    supermicro = Vendor.objects.create(name="Supermicro", published=True)
    _system(dell, "Rack Server", model_number="R750")
    fresh = _system(supermicro, "Rack Server", model_number="R750", published=False)

    assert similar_listings(fresh) == []


def test_two_empty_model_numbers_do_not_match_each_other(dell):
    """Otherwise every listing with a blank model number duplicates every other."""
    _system(dell, "PowerEdge R750", model_number="")
    fresh = _system(dell, "PowerEdge R650", model_number="", published=False)

    assert similar_listings(fresh) == []


def test_a_listing_named_only_after_its_vendor_keeps_a_usable_key():
    """The empty-key trap, which is why ``_name_key`` has a fallback.

    Stripping vendor tokens from "Broadcom" under vendor Broadcom leaves nothing. If
    the key were allowed to be the empty string, every vendor-named listing would
    equal every other one, and an unrelated component would be flagged as a duplicate
    of a NIC because both happened to reduce to nothing.

    So two listings that really are both just the vendor's name match each other, and
    an unrelated sibling does not.
    """
    broadcom = Vendor.objects.create(name="Broadcom", published=True)
    first = Component.objects.create(
        vendor=broadcom, name="Broadcom", kind=ComponentKind.nic.value,
        published=True,
    )
    same = Component.objects.create(
        vendor=broadcom, name="broadcom", kind=ComponentKind.nic.value,
        published=False,
    )
    assert [o for o, _ in similar_listings(same)] == [first]

    unrelated = Component.objects.create(
        vendor=broadcom, name="BCM57414 25GbE", kind=ComponentKind.nic.value,
        published=False,
    )
    assert similar_listings(unrelated) == []


def test_corporate_suffixes_in_a_product_name_are_not_stripped(dell):
    """A known and accepted limit, recorded so it is a decision rather than a
    surprise.

    ``normalize_vendor_name`` understands "Inc" and "Ltd" as company suffixes, but
    that understanding is applied to the *vendor* name to work out which tokens to
    remove from the product name. It is not applied to the product name itself, so a
    listing named "Broadcom Inc" does not reduce to "Broadcom". Product names do not
    normally carry corporate suffixes, and stripping words from them to chase this
    would widen the matcher for a case nobody hits.
    """
    from lumina.hardware.services import _name_key

    assert _name_key("PowerEdge R750", "Dell Inc.") == _name_key(
        "Dell PowerEdge R750", "Dell Inc."
    )
    assert _name_key("Broadcom", "Broadcom") != _name_key("Broadcom Inc", "Broadcom")


def test_a_system_is_never_matched_against_a_component(dell):
    """Different tables and different things. A NIC named "PowerEdge R750" would be
    odd, but it is not a duplicate of the server."""
    Component.objects.create(
        vendor=dell, name="PowerEdge R750", kind=ComponentKind.nic.value,
        published=True,
    )
    fresh = _system(dell, "PowerEdge R750", published=False)

    assert similar_listings(fresh) == []


def test_a_listing_is_not_its_own_duplicate(dell):
    fresh = _system(dell, "PowerEdge R750")

    assert similar_listings(fresh) == []


# --- the reviewer surfaces -----------------------------------------------------


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    client.force_login(user)
    return user


def _submission(listing, submitter):
    return Submission.objects.create(
        submitter=submitter, listing_system=listing,
        claimed_validation_level=ValidationLevel.COMMUNITY,
    )


def test_the_detail_page_warns_about_a_similar_listing(client, reviewer, dell):
    _system(dell, "PowerEdge R750")
    submitter = User.objects.create_user("sub")
    fresh = _system(dell, "Dell PowerEdge R750", published=False)
    submission = _submission(fresh, submitter)

    body = client.get(reverse("review:detail", args=[submission.pk])).content.decode()

    assert "Is this the same hardware as an existing listing?" in body
    assert "same name" in body


def test_the_detail_page_stays_quiet_when_there_is_no_duplicate(
    client, reviewer, dell
):
    submitter = User.objects.create_user("sub")
    submission = _submission(_system(dell, "PowerEdge R650", published=False), submitter)

    body = client.get(reverse("review:detail", args=[submission.pk])).content.decode()

    assert "Is this the same hardware as an existing listing?" not in body


def test_the_queue_flags_the_row(client, reviewer, dell):
    _system(dell, "PowerEdge R750")
    submitter = User.objects.create_user("sub")
    _submission(_system(dell, "PowerEdge R750", published=False), submitter)

    body = client.get(reverse("review:queue")).content.decode()

    assert "1 similar listing" in body


def test_the_queue_does_not_flag_a_clean_row(client, reviewer, dell):
    submitter = User.objects.create_user("sub")
    _submission(_system(dell, "PowerEdge R650", published=False), submitter)

    body = client.get(reverse("review:queue")).content.decode()

    assert "similar listing" not in body


def test_the_queue_costs_the_same_however_many_rows_it_has(client, reviewer, dell):
    """Batched deliberately: a query per row would make the warning's cost scale with
    the queue, and the queue is the page a reviewer keeps open all day.

    Asserted as "doubling the rows does not change the query count" rather than
    against a fixed number, so it measures the property that matters and does not
    fail every time an unrelated panel on the page gains a query.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    submitter = User.objects.create_user("sub")

    def rows(start, stop):
        for index in range(start, stop):
            _submission(
                _system(dell, f"PowerEdge R{index}", published=False), submitter,
            )

    def queries():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(reverse("review:queue")).status_code == 200
        return len(ctx.captured_queries)

    rows(0, 12)
    with_twelve = queries()
    rows(12, 24)
    with_twentyfour = queries()

    assert with_twelve == with_twentyfour, (
        f"{with_twelve} queries for 12 submissions but {with_twentyfour} for 24"
    )


def test_the_annotation_defaults_to_empty_rather_than_missing(dell):
    """Templates read ``s.similar_listings`` directly, and a missing attribute is
    silently falsy in Django templates - so a bug in the annotation would look
    exactly like "no duplicates" rather than failing."""
    submitter = User.objects.create_user("sub")
    submission = _submission(_system(dell, "PowerEdge R750", published=False), submitter)

    annotate_similar_listings([submission])

    assert submission.similar_listings == []


def test_a_recorded_vendor_alias_is_respected(dell):
    """``VendorAlias`` exists so a human's resolution of a vendor string sticks. A
    listing under an aliased vendor row is under the same company."""
    aliased = Vendor.objects.create(name="Dell Computer Corporation", published=False)
    VendorAlias.objects.create(vendor=dell, name="Dell Computer Corporation")
    existing = _system(dell, "PowerEdge R750")
    fresh = _system(aliased, "PowerEdge R750", published=False)

    found = [o for o, _ in similar_listings(fresh)]

    assert found == [existing], (
        "an aliased vendor row was treated as a different company"
    )


# --- warning the submitter, before the fork exists ------------------------------
#
# The reviewer banner above catches a fork after it has been created. This catches it
# before, which is cheaper for everyone: one checkbox instead of a round trip through
# the review queue.


@pytest.fixture
def submit_payload(dell):
    user = User.objects.create_user("dup-submitter", password="pw")
    return user, {
        "kind": "system", "name": "PowerEdge R750", "model_number": "R750",
        "vendor": dell.slug, "claimed_validation_level": ValidationLevel.COMMUNITY,
    }


def test_the_submitter_is_warned_before_a_duplicate_is_created(
    client, dell, submit_payload
):
    existing = _system(dell, "PowerEdge R750")
    user, data = submit_payload
    client.force_login(user)

    resp = client.post(reverse("submit:start"), data)

    assert resp.status_code == 200
    assert not Submission.objects.exists(), "the fork was created anyway"
    body = resp.content.decode()
    assert "Is this already in the catalog?" in body
    assert existing.name in body


def test_the_submitter_can_say_it_is_genuinely_different(
    client, dell, submit_payload
):
    """Not a hard block. The matcher works on hand-typed names and cannot know that two
    machines sharing a normalized name are actually different, so the last word belongs
    to the submitter - but they have to say so explicitly."""
    _system(dell, "PowerEdge R750")
    user, data = submit_payload
    client.force_login(user)

    resp = client.post(
        reverse("submit:start"), dict(data, confirm_not_duplicate="1"),
    )

    assert resp.status_code == 302
    assert Submission.objects.count() == 1


def test_a_clean_submission_is_never_asked_to_confirm(client, dell, submit_payload):
    """The box must not appear on an ordinary submission, or it becomes one more thing
    everybody ticks without reading."""
    user, data = submit_payload
    client.force_login(user)

    resp = client.post(reverse("submit:start"), data)

    assert resp.status_code == 302
    assert Submission.objects.count() == 1


def test_the_warning_does_not_fire_for_an_inline_vendor(client, dell, submit_payload):
    """A vendor created inline is new by definition, so nothing under it can collide.
    Its own duplicate-name check is what covers that case."""
    _system(dell, "PowerEdge R750")
    user, data = submit_payload
    client.force_login(user)

    resp = client.post(reverse("submit:start"), dict(
        data, vendor="__new__", new_vendor_name="Totally New Vendor Ltd",
    ))

    assert resp.status_code == 302


def test_revising_is_not_warned_about_its_own_listing(client, dell):
    """The listing being revised already exists, and would otherwise report itself as
    its own duplicate and make the submission unrevisable."""
    from lumina.releases.models import AlmaLinuxRelease

    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})
    user = User.objects.create_user("reviser", password="pw")
    client.force_login(user)
    client.post(reverse("submit:start"), {
        "kind": "system", "name": "PowerEdge R750", "vendor": dell.slug,
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })
    submission = Submission.objects.get()
    reviewer = User.objects.create_user("dup-rev")
    group, _ = Group.objects.get_or_create(name="reviewer")
    reviewer.groups.add(group)
    submission.request_changes(by=reviewer, reason="tidy the name")

    resp = client.post(reverse("submit:revise", args=[submission.uuid]), {
        "kind": "system", "name": "PowerEdge R750", "vendor": dell.slug,
        "claimed_validation_level": ValidationLevel.COMMUNITY,
    })

    assert resp.status_code == 302
    submission.refresh_from_db()
    assert submission.status == Submission.STATUS_PENDING
