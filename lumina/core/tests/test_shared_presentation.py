"""The presentation both catalogs share, and the drift that made it necessary.

Two things were defined twice and had already diverged:

**The validation badges.** ``lumina-public.css`` and ``lumina-admin.css`` each
carried their own copy, with different colours, so the same tier rendered
differently depending on which page you were on. AlmaLinux-validated was brand
green on the public catalog and ``#831883`` purple in the admin - not a brand
colour at all. The admin copy even carried the comment "shared with public side
colors". They are now one file, ``lumina-levels.css``, loaded by both bases.

**The compatibility card.** Software used Tabler's ``card-table`` and a real
``card-footer`` for its note; hardware wrapped a ``table-sm`` in ``card-body p-0``
and put the note in a padded paragraph. Same information, visibly different
spacing. One partial now renders both, with only the rows per-catalog - software's
carry an HTMX confirm control, hardware's carry a minor floor and a
proven-versus-declared distinction.

Unifying also had to fix a real accessibility defect rather than propagate it:
white text on the brand's darker green measures 2.39:1, well under the 4.5:1 WCAG
AA needs. See ``test_the_almalinux_badge_is_readable``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from lumina.core import certification
from lumina.core.certification import ValidationLevel
from lumina.hardware.models import (
    CommunityAttestation,
    ListingVersion,
    Submission,
    System,
)
from lumina.hardware.services import recompute_listing_levels
from lumina.releases.models import AlmaLinuxRelease
from lumina.software.models import (
    Software,
    SoftwareCertification,
    SoftwareCompatibility,
)
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()

_CSS = Path(settings.BASE_DIR) / "static" / "css"
_TIER_CLASSES = ("badge-community", "badge-vendor", "badge-almalinux")


def _css(name: str) -> str:
    return (_CSS / name).read_text()


def _relative_luminance(hex_colour: str) -> float:
    if len(hex_colour) == 4:                      # #fff -> #ffffff
        hex_colour = "#" + "".join(c * 2 for c in hex_colour[1:])
    channels = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_relative_luminance(a), _relative_luminance(b)),
                       reverse=True)
    return (high + 0.05) / (low + 0.05)


# --- one definition of the tiers ---------------------------------------------


@pytest.mark.parametrize("tier", _TIER_CLASSES)
def test_each_tier_badge_is_defined_exactly_once(tier):
    """Two copies is how the palettes drifted apart in the first place."""
    definitions = [
        name for name in ("lumina-levels.css", "lumina-public.css",
                          "lumina-admin.css")
        if re.search(rf"^\.{tier} ?\{{|^\.{tier}\s*\n?\s*\{{", _css(name), re.M)
    ]

    assert definitions == ["lumina-levels.css"], definitions


@pytest.mark.parametrize(
    "template", ["base_public.html", "base_admin.html"]
)
def test_both_bases_load_the_shared_stylesheet(client, template):
    """A shared file nobody loads is worse than a duplicate: the badges would fall
    back to whatever the framework does with an unknown class."""
    body = (Path(settings.BASE_DIR) / "templates" / template).read_text()

    assert "css/lumina-levels.css" in body


def test_the_shared_stylesheet_stands_alone():
    """It cannot depend on either app stylesheet's variables.

    The two name the same brand colours differently - ``--alx-black-pearl`` versus
    ``--alx-navy``, ``--alx-blue-dark`` versus ``--alx-blue-hover`` - so a file
    loaded by both has to declare what it needs.
    """
    css = _css("lumina-levels.css")
    used = set(re.findall(r"var\((--[\w-]+)\)", css))
    declared = set(re.findall(r"^\s*(--[\w-]+):", css, re.M))

    assert used <= declared, sorted(used - declared)


def test_the_almalinux_badge_is_readable():
    """The defect unifying the palettes surfaced.

    The brand's darker green is light: white on it is 2.39:1, and WCAG AA wants
    4.5:1 for text this size. The badge was effectively unreadable on the public
    catalog. Fixed by darkening the *foreground* - neither brand green is dark
    enough for white text, and altering the background would put an unapproved
    colour on the most authoritative badge in the catalog.
    """
    css = _css("lumina-levels.css")
    green = re.search(r"--lumina-brand-green-dark:\s*(#[0-9a-fA-F]{6})", css).group(1)
    pearl = re.search(
        r"--lumina-brand-black-pearl:\s*(#[0-9a-fA-F]{6})", css
    ).group(1)

    # The brand colour is untouched.
    assert green.lower() == "#68bc11"
    # And it is now paired with a foreground that clears AA.
    assert _contrast(green, pearl) >= 4.5
    assert _contrast(green, "#ffffff") < 4.5  # why white was not an option


def _resolved_vars(css: str) -> dict[str, str]:
    """Every custom property in the file, chased down to a literal colour.

    The file declares tokens in terms of other tokens
    (``--lumina-level-vendor: var(--lumina-brand-blue)``), so a single pass is not
    enough. Two passes cover the depth this file has and the loop is bounded.
    """
    declared = dict(re.findall(r"^\s*(--[\w-]+):\s*([^;]+);", css, re.M))
    for _ in range(4):
        for name, value in list(declared.items()):
            match = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
            if match and match.group(1) in declared:
                declared[name] = declared[match.group(1)]
    return {k: v.strip() for k, v in declared.items()}


@pytest.mark.parametrize("tier", _TIER_CLASSES)
def test_every_tier_badge_clears_wcag_aa(tier):
    """All three, not just the one that was broken."""
    css = _css("lumina-levels.css")
    variables = _resolved_vars(css)

    block = re.search(rf"\.{tier}\s*\{{(.*?)\}}", css, re.S).group(1)

    def colour(declaration: str) -> str:
        # Lookbehind, or "color" also matches the "color" in "background-color".
        raw = re.search(
            rf"(?<![-\w]){declaration}:\s*([^;]+);", block
        ).group(1).strip()
        var = re.fullmatch(r"var\((--[\w-]+)\)", raw)
        return variables[var.group(1)] if var else raw

    background, foreground = colour("background-color"), colour("color")

    ratio = _contrast(background, foreground)
    assert ratio >= 4.5, f"{tier}: {foreground} on {background} is {ratio:.2f}:1"


# --- one compatibility card ---------------------------------------------------


@pytest.fixture
def releases():
    for major in (9, 10):
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True},
        )


@pytest.fixture
def hardware(releases):
    vendor = Vendor.objects.create(name="Dell Inc.", published=True)
    system = System.objects.create(
        vendor=vendor, name="PowerEdge R750", published=True,
    )
    version = ListingVersion.objects.create(
        listing_system=system,
        release=AlmaLinuxRelease.objects.get(major=9),
        source=ListingVersion.SOURCE_RUN,
    )
    user = User.objects.create_user("fan")
    CommunityAttestation.objects.create(
        version=version, listing_system=system,
        level=ValidationLevel.COMMUNITY, attested_by=user,
        submission=Submission.objects.create(
            submitter=user, listing_system=system,
            claimed_validation_level=ValidationLevel.COMMUNITY,
        ),
    )
    recompute_listing_levels(system)
    return system


@pytest.fixture
def software(releases):
    vendor = Vendor.objects.create(
        name="Vaultwise", published=True, scope=Vendor.SCOPE_SOFTWARE,
    )
    product = Software.objects.create(
        vendor=vendor, name="Vaultwise Archive", published=True,
    )
    SoftwareCompatibility.objects.create(
        software=product, release=AlmaLinuxRelease.objects.get(major=9),
    )
    return product


def _card(body: str) -> str:
    anchor = body.index("AlmaLinux compatibility")
    start = body.rindex('<div class="card', 0, anchor)
    return body[start:body.index("</table>", anchor)]


def test_both_catalogs_render_the_same_card_chrome(client, hardware, software):
    """The spacing complaint, pinned. Hardware used ``card-body p-0`` around a
    ``table-sm``; software used ``card-table``. One partial now renders both, so
    they cannot drift again."""
    hardware_card = _card(
        client.get(reverse("hardware:detail", args=[hardware.slug]))
        .content.decode()
    )
    software_card = _card(
        client.get(reverse("software:detail", args=[software.slug]))
        .content.decode()
    )

    for card in (hardware_card, software_card):
        assert 'class="table card-table align-middle"' in card
        assert "table-sm" not in card
        assert 'card-body p-0' not in card


@pytest.mark.parametrize("catalog", ["hardware", "software"])
def test_the_note_is_a_card_footer_on_both(client, hardware, software, catalog):
    """Hardware's note was a padded paragraph inside the body, so it sat at a
    different distance from the table than software's."""
    target = hardware if catalog == "hardware" else software
    url = reverse(f"{catalog}:detail", args=[target.slug])

    body = client.get(url).content.decode()

    assert 'class="card-footer text-secondary small"' in body
    assert "check this table for the release you care about" in body


def test_the_empty_state_is_padded_and_centred(client, releases):
    """Hardware's was a bare cell with no padding, so an empty table looked
    broken rather than empty. Reached by a listing that cites nothing."""
    vendor = Vendor.objects.create(
        name="Nobody", published=True, scope=Vendor.SCOPE_SOFTWARE,
    )
    product = Software.objects.create(
        vendor=vendor, name="Uncited", published=True,
    )

    body = client.get(reverse("software:detail", args=[product.slug])).content.decode()

    assert 'class="text-secondary text-center py-4"' in body
    assert "No AlmaLinux releases cited yet." in body


# --- short tier labels in the tier column -------------------------------------


def test_every_tier_has_a_short_label():
    """The mapping has to cover the enum, or a fourth tier KeyErrors in a template.

    This is the guard that lets ``short_label`` raise on an unknown tier instead of
    falling back to something invented: the failure lands here, at the moment the
    enum grows, rather than on a public page.
    """
    missing = [
        level for level in ValidationLevel if level not in certification.SHORT_LABELS
    ]

    assert not missing, missing


def test_a_blank_tier_passes_through():
    """Hardware's ``ListingVersion.validation_level`` uses ``""`` for "not
    recorded" - blank over NULL, per DJ001 - so the sentinel reaches this filter."""
    assert certification.short_label("") == ""


@pytest.mark.parametrize(
    "catalog,column", [("hardware", "Certified by"), ("software", "Validated by")]
)
def test_the_tier_column_drops_the_validated_suffix(
    client, hardware, software, catalog, column
):
    """The column header already says the badges are validators.

    "Community-validated" under a column headed "Validated by" states it twice per
    cell, and the suffix is the widest part of the badge.

    Both fixtures have community evidence only, so this reaches the ``{% empty %}``
    fallback badge - a literal in each row template, not the filter. The filtered
    loop is ``test_an_official_tier_is_short_too``; the two together cover both
    branches, and each was confirmed to fail when only its own branch was reverted.
    """
    target = hardware if catalog == "hardware" else software
    card = _card(
        client.get(reverse(f"{catalog}:detail", args=[target.slug])).content.decode()
    )

    assert column in card
    # The badge's own text, not merely the word somewhere on the card.
    assert re.search(r"badge-community[^>]*>\s*Community\s*<", card), card
    assert "-validated" not in card, "the suffix is still in the tier column"


@pytest.mark.parametrize("catalog", ["hardware", "software"])
def test_an_official_tier_is_short_too(client, releases, catalog):
    """Not just the community fallback. The two catalogs reach the badge by
    different routes - hardware loops ``official_levels`` off its attestations,
    software loops real ``SoftwareCertification`` rows - so both are exercised."""
    release = AlmaLinuxRelease.objects.get(major=9)
    if catalog == "hardware":
        vendor = Vendor.objects.create(name="Vendorly", published=True)
        target = System.objects.create(
            vendor=vendor, name="Certified Box", published=True,
        )
        version = ListingVersion.objects.create(
            listing_system=target, release=release, source=ListingVersion.SOURCE_RUN,
        )
        user = User.objects.create_user("vendor-rep")
        CommunityAttestation.objects.create(
            version=version, listing_system=target,
            level=ValidationLevel.VENDOR, attested_by=user,
            submission=Submission.objects.create(
                submitter=user, listing_system=target,
                claimed_validation_level=ValidationLevel.VENDOR,
            ),
        )
        recompute_listing_levels(target)
    else:
        vendor = Vendor.objects.create(
            name="Vendorly Soft", published=True, scope=Vendor.SCOPE_SOFTWARE,
        )
        target = Software.objects.create(
            vendor=vendor, name="Certified App", published=True,
        )
        SoftwareCertification.objects.create(
            compatibility=SoftwareCompatibility.objects.create(
                software=target, release=release,
            ),
            level=ValidationLevel.VENDOR,
        )

    card = _card(
        client.get(reverse(f"{catalog}:detail", args=[target.slug])).content.decode()
    )

    assert re.search(r"badge-vendor[^>]*>\s*Vendor\s*<", card), card
    assert "-validated" not in card


def test_a_standalone_badge_keeps_the_long_label(client, hardware):
    """Scope, pinned. The suffix is dropped *because* a column header carries the
    meaning; the headline badge on a detail page has no header to lean on, so
    stripping it there would leave a bare word claiming nothing in particular."""
    body = client.get(reverse("hardware:detail", args=[hardware.slug])).content.decode()

    assert "Community-validated" in body[:body.index("AlmaLinux compatibility")]


def test_the_short_labels_are_defined_once(client, hardware):
    """The certification results table had grown its own inline
    ``{% if run.trust_level == "almalinux" %}AlmaLinux{% elif ... %}``."""
    for name in ("hardware/detail.html", "hardware/_compatibility_row.html",
                 "software/_compatibility_row.html"):
        source = (Path(settings.BASE_DIR) / "templates" / name).read_text()
        rendered = re.sub(r"{% comment %}.*?{% endcomment %}", "", source, flags=re.S)

        assert 'AlmaLinux{% e' not in rendered, f"{name} hardcodes a tier label"


def test_each_catalog_keeps_its_own_columns(client, hardware, software):
    """Shared chrome, not shared content. Software has a fourth column for its
    confirm control; hardware has no equivalent, because attesting to hardware
    means running the suite."""
    hardware_card = _card(
        client.get(reverse("hardware:detail", args=[hardware.slug]))
        .content.decode()
    )
    software_card = _card(
        client.get(reverse("software:detail", args=[software.slug]))
        .content.decode()
    )

    assert "Community confirmations" in hardware_card
    assert hardware_card.count("<th>") == 3
    assert software_card.count("<th>") == 4
