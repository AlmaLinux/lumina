"""A run published anonymously names nobody, on any public page it reaches.

Reported as: the system and its run are anonymous, but not the association to components -
an information leak, where anonymous should mean anonymous everywhere.

``TestRun.public_submitter_name`` is the gate, and its own docstring names the failure: a
public surface that reads ``submitter.username`` directly "is a leak, and it looks exactly like
correct code." Two did. The certification-results table on ``hardware/detail.html``, which
renders for system *and* component listings, printed the username in its "Validated by" column;
and the benchmark leaderboard's per-run rows printed it beside the score. So a person who asked
not to be named was named against every part their machine was certified with, and against
every benchmark they ran - the case the anonymity option exists for most.

Tested by rendering the pages and searching the HTML for the username, rather than by checking
that a template uses the right accessor. A leak can come through a view's context as easily as
a template, and only the rendered page says what a reader actually receives.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from lumina.hardware.models import ComponentKind
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db

# Distinctive enough that finding it anywhere in a page is unambiguous: no header, nav item,
# or fixture string can contain it by coincidence.
SECRET = "quietly-anonymous-7731"


@pytest.fixture
def anonymous_submitter():
    return User.objects.create_user(SECRET)


@pytest.fixture
def reviewer():
    user = User.objects.create_user("anon-rev")
    user.groups.add(Group.objects.get_or_create(name="reviewer")[0])
    return user


def _approved(submitter, reviewer, *, inventory=None, run_types=("validate",), results=None,
              run_id=None, anonymous=True) -> TestRun:
    kw = {"run_id": run_id} if run_id else {}
    run = ingest.ingest_bundle(
        submitter=submitter, source="api", publish_anonymously=anonymous,
        bundle_file=f.as_upload(f.build_bundle(f.make_report(
            run_types=list(run_types), inventory=inventory or f.custom_build_inventory(),
            results=results or [f.validate_result("validate.cpu.functional")], **kw,
        ))),
    )
    run = release(TestRun.objects.get(pk=run.pk))
    services.approve_run(run, by=reviewer)
    services.create_listings_from_run(run, by=reviewer)
    run.refresh_from_db()
    assert run.publish_anonymously is anonymous, "the premise: the attribution asked for"
    assert run.is_public, "the premise: a published one, which is what a reader can see"
    return run


def test_the_run_page_was_already_anonymous(client, anonymous_submitter, reviewer):
    """The control: the half the report says works. If this fails the fixture is wrong, not
    the pages below."""
    run = _approved(anonymous_submitter, reviewer)

    body = client.get(run.get_absolute_url()).content.decode()

    assert "Anonymous" in body
    assert SECRET not in body


def test_the_system_page_names_nobody(client, anonymous_submitter, reviewer):
    """The certification-results table is shared with component pages, so the system page
    leaked through the same line - the report's "the system is anonymous" held for the
    listing's own fields, not for the evidence table underneath them."""
    run = _approved(anonymous_submitter, reviewer)

    body = client.get(run.listing_system.get_absolute_url()).content.decode()

    assert SECRET not in body


@pytest.mark.parametrize("kind", [ComponentKind.motherboard, ComponentKind.cpu])
def test_a_component_page_names_nobody(client, anonymous_submitter, reviewer, kind):
    """The report. Every part an anonymous run certified listed that run's submitter by name,
    so the person was identifiable from any component of their machine."""
    run = _approved(anonymous_submitter, reviewer)
    component = run.listing_components.get(kind=kind.value)
    assert component.published, "the premise: a component a reader can open"

    body = client.get(component.get_absolute_url()).content.decode()

    assert SECRET not in body
    assert "Anonymous" in body


def test_a_benchmark_leaderboard_names_nobody(client, anonymous_submitter, reviewer):
    """The case the anonymity option exists for most, per the comment on the submit form:
    a score is the thing a person is likeliest to want unattributed."""
    bench = "bench.cpu.sysbench-multi"
    _approved(
        anonymous_submitter, reviewer, run_types=("benchmark",),
        inventory=f.default_inventory(),
        results=[f.benchmark_result(test_id=bench)],
        run_id="77310000-0000-0000-0000-000000000001",
    )

    url = reverse("benchmarks:leaderboard", args=[bench])
    body = client.get(url, {"group": "none"}).content.decode()

    assert "bench-value" in body, "the premise: the per-run rows rendered"
    assert SECRET not in body
    assert "Anonymous" in body


def test_an_attributed_run_is_still_attributed(client, reviewer):
    """The fix must hide the anonymous, not everybody. A page that names nobody at all would
    pass every test above."""
    named = User.objects.create_user("happily-named-4420")
    run = _approved(named, reviewer, anonymous=False)
    component = run.listing_components.get(kind=ComponentKind.cpu.value)

    body = client.get(component.get_absolute_url()).content.decode()

    assert "happily-named-4420" in body


# --- the next template ------------------------------------------------------------

# Template directories whose pages are not public. Reviewer and admin pages show the real
# submitter on purpose - a reviewer judging evidence has to know whose it is - and an account
# page shows its own owner their own name.
_NOT_PUBLIC = ("review/", "admin/", "accounts/")

# Public templates allowed a raw read, each because it is gated to the people allowed to see it.
_GATED = {
    # Shows reviewers the real name beside a "Published as Anonymous" badge, behind
    # ``request.user|is_reviewer``; everybody else gets ``public_submitter_name``.
    "results/_run_summary.html",
}

_RAW_IDENTITY = __import__("re").compile(
    r"submitter\.(username|get_username)|submitter\s*(\}\}|\|)")


def test_no_public_template_reads_the_submitter_directly():
    """The two leaks were one line each and read like correct code, which is the hazard the
    gate's own docstring warns about. The render tests above prove the pages that exist today;
    this stops the next public template from adding a third.

    Comment blocks are stripped first: this codebase explains itself in templates, and prose
    saying "never ``submitter.get_username``" is the opposite of a leak.
    """
    import re

    from django.conf import settings

    root = settings.BASE_DIR / "templates"
    offenders = []
    for path in sorted(root.rglob("*.html")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(_NOT_PUBLIC) or rel in _GATED:
            continue
        text = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", path.read_text(),
                      flags=re.S)
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
        for match in _RAW_IDENTITY.finditer(text):
            line = text[:match.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}: {match.group(0)}")

    assert not offenders, (
        "public templates reading the submitter directly, which names people who asked to "
        f"be anonymous: {offenders}. Use run.public_submitter_name."
    )
