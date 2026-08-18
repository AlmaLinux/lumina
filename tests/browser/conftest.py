"""Fixtures for the tests that drive a real browser.

Why these exist at all: this project has shipped broken interface repeatedly, and every time the
server-side suite stayed green because the markup was correct and the *rendering* was not. A card
whose contents fell outside it, a stylesheet one of the two layouts never linked, a glyph from a
font that page does not load, a button attached to no form. All of those are green in a response
body assertion and obvious in a browser.

Two rules shape the harness:

**It has to run in CI, not only on a maintainer's laptop.** The browser is a *system package*
everywhere: ``chromium`` from EPEL on the AlmaLinux runners, ``chromium-browser`` on a Fedora
workstation. Playwright is used as the driver only, never as a browser distributor, so nothing
here downloads a 150 MB bundle into a container image or a home directory. Resolution order is
``LUMINA_BROWSER_EXECUTABLE``, then the usual system paths, then Playwright's own managed browser
as a last resort for anyone who has installed one.

**A failure has to be diagnosable.** Every helper reports what it looked at, screenshots land in
an artifact directory on failure, and the sign-in shortcut is the only thing that skips the real
interface, because signing in goes through Keycloak in production and there is nothing of ours to
test on that path.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# Playwright's synchronous API drives the browser from a greenlet with an asyncio loop running in
# this thread. Django's ORM refuses to run with a loop present, on the assumption that a blocking
# query inside an event loop is a mistake. Here it is not: the loop belongs to the browser driver,
# every query in these tests is on the test's own thread, and the alternative is rewriting every
# fixture against the async ORM to work around a guard aimed at a different problem.
#
# Set before anything imports Django models, because the check reads the environment each call and
# the fixtures below run queries as soon as they are collected.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

try:
    from playwright.sync_api import Browser, sync_playwright
except ImportError:  # pragma: no cover - exercised by anyone without the extra installed
    # Ignore the directory rather than erroring during collection. Somebody who installed only the
    # dev extra should be able to run the ordinary suite without being told their checkout is
    # broken, and ``pytest.importorskip`` inside a conftest is not the way to arrange that: it
    # raises during import, which reads as a collection error on some versions.
    collect_ignore_glob = ["test_*.py"]
    Browser = object
    sync_playwright = None

# Where a failing test drops its screenshot and its HTML. Overridable so CI can point it at the
# directory it uploads as an artifact.
ARTIFACTS = Path(os.environ.get("LUMINA_BROWSER_ARTIFACTS", "/tmp/lumina-browser"))

# Tried in order after the managed browser. Fedora, Debian/Ubuntu, and the Chrome packages.
SYSTEM_CHROMIUM = (
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)

# Every browser test runs against a live server, and requesting ``live_server`` pulls in
# transactional database semantics whether or not a test asks for them.
pytestmark = pytest.mark.browser


def _system_chromium() -> str | None:
    for candidate in SYSTEM_CHROMIUM:
        if Path(candidate).exists() or shutil.which(candidate):
            return candidate
    return None


def _launch(playwright):
    """A Chromium, preferring the one the operating system installed.

    System first, deliberately. The CI runners are AlmaLinux with root, so ``dnf install chromium``
    from EPEL is a line in the image and the same package a maintainer has locally. Letting
    Playwright fetch its own would put a second, differently versioned browser in the image for no
    gain, and would make the suite depend on a download at test time.

    ``--no-sandbox`` because the default sandbox wants user namespaces that a CI container usually
    does not grant, and the only pages loaded are our own templates on a loopback address.
    """
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    executable = os.environ.get("LUMINA_BROWSER_EXECUTABLE") or _system_chromium()
    if executable:
        # Raises rather than skips, deliberately. Naming a browser and having it fail to start is
        # a broken configuration, and CI sets the variable precisely so a missing package fails
        # the job instead of quietly skipping the tests it was added to run.
        return playwright.chromium.launch(executable_path=executable, args=args)
    try:
        # Only for a machine that has run ``playwright install`` and has no system package.
        return playwright.chromium.launch(args=args)
    except Exception as error:  # noqa: BLE001 - turned into a skip, not an error
        # A skip, not a failure. These run with the rest of the suite now, so somebody who has not
        # installed Chromium yet should get a clear sentence about it rather than a wall of red
        # from tests that were never going to run on their machine.
        pytest.skip(
            "no Chromium found, so the browser tests cannot run. Install the system package "
            "(dnf install chromium, after epel-release on AlmaLinux; apt install chromium on "
            f"Debian), or set LUMINA_BROWSER_EXECUTABLE. Playwright said: {error}"
        )


@pytest.fixture(scope="session")
def _serialized_reference_data(django_db_setup, django_db_blocker):
    """The snapshot Django takes of the freshly migrated database, kept somewhere it survives.

    Django stores it as an attribute on the *connection wrapper* object, and restores from it only
    ``if hasattr(connection, "_test_serialized_contents")``. Starting a live server closes and
    replaces those wrappers, so the attribute goes with them. Nothing raises: the restore is
    skipped, the ``hasattr`` is simply False, and from the first page load onward every test runs
    against a database holding nothing but what its own fixtures put there. A curated CPU family
    is gone, so the vendor claim control is not rendered, so a test about that control finds
    nothing and passes.

    Captured once, here, before any server has started.
    """
    from django.db import connections

    with django_db_blocker.unblock():
        return {
            alias: getattr(connections[alias], "_test_serialized_contents", None)
            for alias in connections
        }


@pytest.fixture
def _reference_data(_serialized_reference_data, django_db_blocker):
    """Put the snapshot back on whatever connection wrapper exists now.

    Ordered before the database fixtures below, because Django reads the attribute during its own
    fixture setup, which happens when ``transactional_db`` is resolved.
    """
    from django.db import connections

    with django_db_blocker.unblock():
        for alias, blob in _serialized_reference_data.items():
            if blob and not hasattr(connections[alias], "_test_serialized_contents"):
                connections[alias]._test_serialized_contents = blob


@pytest.fixture(autouse=True)
def _database(_reference_data, transactional_db, django_db_serialized_rollback):
    """Every browser test gets a committed, thread-shareable database.

    ``transactional_db`` rather than ``db``, and not optionally. A live server answers each request
    on its own thread with its own connection, so the test's work has to be committed to be
    visible at all. Left on the default, two things went wrong at once and neither announced
    itself: the reference data the migrations inserted vanished after the first test that loaded a
    page, so every later test rendered a valid page against an empty catalog and asserted happily
    against interface that was simply absent; and the request threads raced each other closing one
    shared SQLite connection, which segfaulted the interpreter.

    ``django_db_serialized_rollback`` because the price of transactional semantics is a truncation
    after every test, migration data included. This restores it. It is the reason a curated CPU
    family is still there for the second test to match against.
    """


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        instance = _launch(playwright)
        yield instance
        instance.close()


@pytest.fixture
def page(browser: Browser, request):
    """A fresh context per test, so cookies and storage never leak between them.

    The viewport is fixed. Layout assertions are meaningless against a viewport that varies by
    machine, and a fixed one is also what makes "the body must not scroll sideways" a real claim
    rather than a guess about somebody's monitor.
    """
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.set_default_timeout(5000)
    tab = context.new_page()
    errors: list[str] = []
    tab.on("pageerror", lambda exc: errors.append(str(exc)))
    yield tab
    if request.node.rep_call is not None and request.node.rep_call.failed:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        stem = ARTIFACTS / request.node.name.replace("/", "_")[:120]
        tab.screenshot(path=f"{stem}.png", full_page=True)
        Path(f"{stem}.html").write_text(tab.content())
        print(f"\nbrowser artifacts: {stem}.png and {stem}.html")
    context.close()
    assert not errors, "the page raised a JavaScript error:\n" + "\n".join(errors)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Let the ``page`` fixture see whether its test failed, so it can save the evidence."""
    outcome = yield
    setattr(item, f"rep_{call.when}", outcome.get_result())


@pytest.fixture(autouse=True)
def _no_report_attr(request):
    for phase in ("setup", "call", "teardown"):
        if not hasattr(request.node, f"rep_{phase}"):
            setattr(request.node, f"rep_{phase}", None)


@pytest.fixture
def sign_in(page, live_server):
    """Put a user's session cookie in the browser. The one shortcut past the real interface.

    Signing in is Keycloak's job in every deployment that matters, and the local password form
    exists only because OIDC is absent in devstack. Driving it would test a login page production
    does not have, at the cost of a form round trip in every single test.
    """
    from importlib import import_module

    from django.conf import settings

    def _sign_in(user):
        engine = import_module(settings.SESSION_ENGINE)
        session = engine.SessionStore()
        session["_auth_user_id"] = str(user.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session["_auth_user_hash"] = user.get_session_auth_hash()
        session.save()
        page.context.add_cookies([{
            "name": settings.SESSION_COOKIE_NAME,
            "value": session.session_key,
            "url": live_server.url,
        }])
        return user

    return _sign_in


@pytest.fixture
def visit(page, live_server):
    """Open a named URL and wait for the page to settle.

    ``reverse`` rather than a literal path, so a URL rename breaks these the same way it breaks
    everything else rather than leaving them quietly testing a 404 page.
    """
    from django.urls import reverse

    def _visit(url_name, *args, **query):
        from urllib.parse import urlencode

        path = reverse(url_name, args=args)
        if query:
            path = f"{path}?{urlencode(query, doseq=True)}"
        response = page.goto(f"{live_server.url}{path}", wait_until="networkidle")
        assert response is not None and response.status < 400, (
            f"{url_name} returned {response.status if response else 'nothing'}"
        )
        return page

    return _visit


# --- data ----------------------------------------------------------------------------
#
# Defined here rather than imported into each test file. Importing a fixture by name shadows the
# parameter of the same name, which every linter reports and which would hide a real redefinition;
# registering the module with ``pytest_plugins`` is not allowed outside the top-level conftest.
#
# Built through the same ingest and service functions the application uses, never by hand through
# the ORM. A browser test that starts from hand-made rows proves the templates work against data
# the application does not produce.

from django.contrib.auth.models import Group, User  # noqa: E402

from lumina.releases.models import AlmaLinuxRelease  # noqa: E402
from lumina.vendors.models import Vendor, VendorMembership  # noqa: E402
from tests.browser.fixtures import make_run  # noqa: E402


@pytest.fixture
def releases(db):
    return [
        AlmaLinuxRelease.objects.get_or_create(
            major=major, defaults={"supported": True, "latest_minor": 8},
        )[0]
        for major in (9, 10)
    ]


@pytest.fixture
def intel(db):
    vendor, _ = Vendor.objects.get_or_create(
        name="Intel", defaults={"published": True},
    )
    vendor.verified = True
    vendor.save(update_fields=["verified"])
    return vendor


@pytest.fixture
def dell(db):
    vendor, _ = Vendor.objects.get_or_create(
        name="Dell Inc.", defaults={"published": True, "verified": True},
    )
    return vendor


@pytest.fixture
def reviewer(db):
    user = User.objects.create_user("browser-reviewer", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


@pytest.fixture
def submitter(db):
    return User.objects.create_user("browser-submitter", password="pw")


@pytest.fixture
def vendor_engineer(db, intel):
    """A submitter who speaks for Intel, so the per-part vendor claim is offered."""
    user = User.objects.create_user("browser-intel", password="pw")
    VendorMembership.objects.create(
        user=user, vendor=intel, role=VendorMembership.ROLE_SUBMITTER,
    )
    return user


@pytest.fixture
def pending_run(releases, dell, vendor_engineer):
    """A run sitting in the reviewer's queue, submitted by somebody who speaks for Intel.

    The shape most of the reviewer flows need: a machine that is not in the catalog yet, so
    approving creates the listing, and a CPU whose vendor the submitter represents, so the
    per-part claim control is rendered.
    """
    run = make_run(vendor_engineer)
    run.listing_proposal = {
        "vendor_name": "Dell Inc.", "name": "PowerEdge R760", "machine_kind": "prebuilt",
    }
    run.status = run.STATUS_PENDING
    run.save(update_fields=["listing_proposal", "status"])
    return run


@pytest.fixture
def published_system(releases, dell):
    """A live catalog entry, for the public-facing flows."""
    from lumina.hardware.models import ListingVersion, System

    system = System.objects.create(
        vendor=dell, name="PowerEdge R760", published=True,
        description="2U dual-socket rack server.",
    )
    ListingVersion.objects.create(listing_system=system, release=releases[0])
    return system


@pytest.fixture
def archived_and_active(releases, dell, submitter):
    """One draft left alone and one put away, so the dashboard has both panes to show."""
    from lumina.results import services

    active = make_run(submitter)
    put_away = make_run(submitter, run_id="ffffffff-0000-0000-0000-000000000001")
    services.archive_run(put_away, by=submitter)
    return {"active": active, "archived": put_away}
