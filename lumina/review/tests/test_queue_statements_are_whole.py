"""The free text a reviewer is deciding on is shown in full on the review queue.

Reported as not being able to see all of the text submitted on a vendor claim. The note was
rendered through ``truncatechars:90`` and the remainder was simply unreachable - no link, no
tooltip, no detail page. The survey token request's justification had the identical problem at
160 characters.

Both are the *evidence*, not a preview of it. Approving a vendor claim hands that person
ownership of the vendor and every unmaintained listing attributed to it, on the strength of
the case they made in that box; approving a token request lifts the 30-day token limit for
that account. Neither model has a detail view, so the queue is the only page either one is
ever shown on, and a reviewer who reached the cut-off had nowhere else to go.

Asserted on a statement longer than the old limits rather than on the absence of an ellipsis:
a test that checked for "…" would pass the moment somebody switched to ``truncatewords``.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.survey.models import SurveyTokenRequest
from lumina.vendors.models import Vendor, VendorClaim

pytestmark = pytest.mark.django_db

# Comfortably past both old cut-offs (90 and 160), and the shape of the real thing: somebody
# explaining who they are and why they should be trusted with a vendor.
LONG_STATEMENT = (
    "I am an employee of GIGABYTE Technology Co., Ltd. and am responsible for the AlmaLinux "
    "certification programme across our server and workstation lines. I can be reached at my "
    "corporate address and our procurement team can confirm my role on request. We intend to "
    "submit validation runs for the full B850 and H610 ranges over the next two quarters."
)


@pytest.fixture
def reviewer(client):
    user = User.objects.create_user("stmt-rev", password="pw", is_staff=True)
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    client.force_login(user)
    return user


def _queue(client) -> str:
    return client.get(reverse("review:queue")).content.decode()


def test_a_vendor_claim_note_is_not_cut_off(client, reviewer):
    """The report, with the claim the reporter was actually looking at."""
    vendor = Vendor.objects.create(name="Gigabyte Technology Co., Ltd.")
    VendorClaim.objects.create(
        vendor=vendor, requester=User.objects.create_user("chiatien"),
        work_email="jerry.lin@gigabyte.example", role_at_vendor="Quality Validation Manager",
        note=LONG_STATEMENT,
    )

    assert LONG_STATEMENT in _queue(client)


def test_a_survey_token_justification_is_not_cut_off(client, reviewer):
    """The same bug, one tab along. Found by looking rather than by being reported, which is
    the only reason it is not a second report."""
    SurveyTokenRequest.objects.create(
        requester=User.objects.create_user("fleet-op"), justification=LONG_STATEMENT,
    )

    assert LONG_STATEMENT in _queue(client)


def test_the_cell_is_given_a_readable_measure(client, reviewer):
    """Shown whole is not the same as shown well. Without a bound the cell stretches until
    the Decision column is pushed off the side of the table, which trades one unreadable
    thing for another - so the class carrying the width is part of the fix."""
    vendor = Vendor.objects.create(name="Gigabyte Technology Co., Ltd.")
    VendorClaim.objects.create(
        vendor=vendor, requester=User.objects.create_user("measured"),
        work_email="m@example.com", role_at_vendor="QA", note=LONG_STATEMENT,
    )

    assert "review-statement" in _queue(client)
    # And the class has a rule behind it. A name in the markup that no stylesheet defines
    # styles nothing, and the cell goes back to stretching - which the markup assertion
    # above cannot tell apart from the fix working.
    css = (settings.BASE_DIR / "static/css/lumina-admin.css").read_text()
    assert ".review-statement" in css
    assert "max-width" in css.split(".review-statement")[1].split("}")[0]


def test_line_breaks_in_a_statement_survive(client, reviewer):
    """Somebody laying out their case over several lines gets those lines. Rendered into one
    paragraph it reads as a wall, which is the readability half of the same complaint."""
    vendor = Vendor.objects.create(name="Gigabyte Technology Co., Ltd.")
    VendorClaim.objects.create(
        vendor=vendor, requester=User.objects.create_user("multiline"),
        work_email="m@example.com", role_at_vendor="QA",
        note="First line about who I am.\nSecond line about what we ship.",
    )

    assert "<br>" in _queue(client)


def test_a_statement_is_still_escaped(client, reviewer):
    """``linebreaksbr`` keeps autoescaping, and this is attacker-supplied text on a staff page
    - so the thing that makes the newlines work must not also make the markup work."""
    vendor = Vendor.objects.create(name="Gigabyte Technology Co., Ltd.")
    VendorClaim.objects.create(
        vendor=vendor, requester=User.objects.create_user("injector"),
        work_email="m@example.com", role_at_vendor="QA",
        note="<script>alert(1)</script>",
    )

    body = _queue(client)

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
