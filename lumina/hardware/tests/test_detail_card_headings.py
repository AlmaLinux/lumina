"""A card heading that carries a count renders the same way wherever it appears.

Reported twice about the same card. First "Used in systems1" - the count was a sibling
``card-subtitle`` span, which Tabler lays out inline with no gap at all. Moving it into the
heading fixed the run-together and introduced the second report: ``.card-title`` carries its own
bottom margin, so without ``mb-0`` the header opened up below the heading and the card looked
wrong in a new way.

"Certification results (2)" on the same page had both halves right the whole time. These hold
the two cards to one pattern, because eyeballing it got it wrong twice.
"""
from __future__ import annotations

import re

import pytest
from django.contrib.auth.models import User

from lumina.hardware.models import Component, ComponentKind, System
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db

# The heading, its count, and the classes on each - captured so a change to either is visible.
HEADING = re.compile(
    r'<h3 class="(?P<classes>[^"]*card-title[^"]*)">\s*'
    r'(?P<text>[^<]*?)\s*'
    r'<span class="(?P<count_classes>[^"]*)">\((?P<count>\d+)\)</span>\s*</h3>',
    re.S,
)


@pytest.fixture
def component():
    """A part used in one system, so both counted cards render."""
    vendor = Vendor.objects.create(name="Heading Co", published=True)
    part = Component.objects.create(
        vendor=vendor, name="NIC 9000", kind=ComponentKind.nic.value, published=True,
        created_by=User.objects.create_user("heading-owner"),
    )
    system = System.objects.create(vendor=vendor, name="Box 1", published=True)
    system.related_components.add(part)
    return part


def headings(client, component) -> dict[str, re.Match]:
    body = client.get(component.get_absolute_url()).content.decode()
    return {match.group("text"): match for match in HEADING.finditer(body)}


def test_the_count_is_separated_from_the_heading(client, component):
    """"Used in systems1" was the report. The count is its own element inside the heading, so
    the space between them is a space and not a layout accident."""
    found = headings(client, component)

    assert "Used in systems" in found, "the counted heading did not render"
    assert found["Used in systems"].group("count") == "1"


def test_the_heading_does_not_open_a_gap_beneath_itself(client, component):
    """``.card-title`` has a bottom margin of its own, which inside a ``card-header`` shows as
    dead space under the heading. Every counted heading resets it."""
    for text, match in headings(client, component).items():
        assert "mb-0" in match.group("classes"), f"{text!r} is missing mb-0"


def test_the_note_belongs_to_the_heading_and_not_to_a_band_of_its_own(client, component):
    """The third report about this card, and the one the first two were hiding. The note sat in
    a ``card-body`` between the header and the list, so a one-line explanation was walled off
    in its own stripe between two dividers and the card read as three unrelated bands.

    It is a ``card-subtitle`` in the header now, as ``submit/start.html`` does it throughout.
    Asserting on the order is what catches a regression: the note has to come before the list
    it explains, and inside the header rather than after it.
    """
    body = client.get(component.get_absolute_url()).content.decode()
    card = body[body.index("Used in systems"):]
    card = card[:card.index("</div>\n    </div>")] if "</div>\n    </div>" in card else card

    note = card.index("Systems this part is recorded in")
    assert "card-subtitle" in card[:note], "the note is not a subtitle of the heading"
    assert "card-body" not in card[:note], "the note is still in a band of its own"


def test_every_counted_heading_uses_the_one_pattern(client, component):
    """Spelled out rather than compared between cards, because the two do not always appear on
    the same page - a part with no runs has no certification results to count. The pattern is
    the thing being held: the heading resets its margin, and the count is quiet beside it."""
    found = headings(client, component)

    assert found
    for text, match in found.items():
        assert match.group("classes") == "card-title mb-0", text
        assert match.group("count_classes") == "text-secondary fw-normal", text
