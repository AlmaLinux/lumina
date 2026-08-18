"""Dashboard workspace sections: my systems / components / runs."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("runner", password="x")


def _validate_run(submitter, **kw):
    report = f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        **kw,
    )
    return ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(report)),
        source="api",
    )


def test_dashboard_lists_my_work_and_not_others(client, submitter):
    reviewer = User.objects.create_user(
        "rev", password="x", is_staff=True, is_superuser=True
    )
    from django.contrib.auth.models import Group

    group, _ = Group.objects.get_or_create(name="reviewer")
    reviewer.groups.add(group)

    mine = _validate_run(submitter)
    services.approve_run(release(mine), by=reviewer)  # ties CPU/GPU/board components too
    other_user = User.objects.create_user("other", password="x")
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    System.objects.create(vendor=dell, name="Someone Elses Server",
                          created_by=other_user)

    client.force_login(submitter)
    resp = client.get(reverse("accounts:dashboard"))
    text = resp.text
    # Named for what they are: runs this person performed, not hardware they own.
    assert "My system validations" in text
    assert "My component validations" in text
    assert "My validation runs" in text
    assert "My benchmark runs" in text
    # the run's auto-tied components appear as the submitter's work; the CPU
    # rolls up to its seeded family
    assert "Intel Xeon Scalable 4th Generation" in text
    # other people's listings do not
    assert "Someone Elses Server" not in text
    # Whether the lists carry search boxes is about their length, and has a test of its own.


def test_a_draft_listing_is_named_but_not_linked(client, submitter):
    """``hardware:detail`` refuses an unpublished listing, so linking one from the
    dashboard produced a 404 on the owner's own work.

    The same defect the software table had. A draft is still listed, because the
    owner needs to see it exists; it just stops pretending to be a page.
    """
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    draft = System.objects.create(
        vendor=dell, name="Draft Server", created_by=submitter, published=False,
    )
    live = System.objects.create(
        vendor=dell, name="Live Server", created_by=submitter, published=True,
    )
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).text

    assert "Draft Server" in body
    assert reverse("hardware:detail", args=[draft.slug]) not in body
    # Not over-broad: a published listing is still a link.
    assert reverse("hardware:detail", args=[live.slug]) in body
    # The reason it cannot be linked.
    assert client.get(reverse("hardware:detail", args=[draft.slug])).status_code == 404


def test_a_draft_component_is_named_but_not_linked(client, submitter):
    from lumina.hardware.models import Component, ComponentKind

    intel, _ = Vendor.objects.get_or_create(name="Intel")
    draft = Component.objects.create(
        vendor=intel, name="Draft NIC", kind=ComponentKind.nic.value,
        created_by=submitter, published=False,
    )
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).text

    assert "Draft NIC" in body
    assert reverse("hardware:detail", args=[draft.slug]) not in body


def test_a_run_against_a_draft_system_does_not_link_it(client, submitter):
    """The runs table links its system too, and a run can be attached to a
    listing before that listing is published."""
    dell, _ = Vendor.objects.get_or_create(name="Dell Inc.")
    draft = System.objects.create(
        vendor=dell, name="Draft Host", created_by=submitter, published=False,
    )
    run = _validate_run(submitter)
    run.listing_system = draft
    run.save(update_fields=["listing_system"])
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).text

    assert "Draft Host" in body
    assert reverse("hardware:detail", args=[draft.slug]) not in body


def test_dashboard_splits_benchmarks_from_validations(client, submitter):
    _validate_run(submitter)
    bench_report = f.make_report(run_types=["benchmark"],
                                 results=[f.benchmark_result()])
    ingest.ingest_bundle(
        submitter=submitter,
        bundle_file=f.as_upload(f.build_bundle(bench_report)),
        source="api",
    )
    client.force_login(submitter)
    resp = client.get(reverse("accounts:dashboard"))

    # Asserted on the context, not the headings. Both headings sit outside their
    # ``{% if %}``, so the old version passed on a database with no runs at all and
    # would not have noticed the two querysets being swapped in the view.
    validations = list(resp.context["sections"]["validation"].page)
    benchmarks = list(resp.context["sections"]["benchmark"].page)
    assert [r.run_type for r in validations] == ["validate"]
    assert [r.run_type for r in benchmarks] == ["benchmark"]
    assert "My validation runs" in resp.text and "My benchmark runs" in resp.text


# --- strings alma-certify quotes back to the submitter ---------------------------
#
# When a validation run is uploaded, alma-certify prints an ACTION REQUIRED block
# telling the submitter where to finish it, and it names these page elements
# verbatim so the instruction can be followed by eye:
#
#     https://<server>/my/
#     under "My validation runs", listed as "Awaiting submitter details"
#     with "Finish submission" in the Action column.
#
# Those strings live in the suite repository
# (``alma_certify/submit/client.py``: DASHBOARD_PATH, DASHBOARD_TABLE,
# _DRAFT_STATUS_LABEL, DASHBOARD_ACTION), which cannot import from here. So
# renaming any of them on this side turns a confident instruction into a wrong
# one, with nothing to notice. These tests fail instead - and the fix is to
# update the suite to match, not to loosen the assertion.


def test_the_dashboard_lives_where_alma_certify_says_it_does():
    assert reverse("accounts:dashboard") == "/my/"


def test_the_dashboard_names_the_elements_alma_certify_cites(client, submitter):
    """A freshly uploaded validation run is a draft, which is the state the
    ACTION REQUIRED block is written for."""
    from lumina.results.models import TestRun

    run = _validate_run(submitter)
    assert run.status == TestRun.STATUS_DRAFT, "not the state being described"
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    for quoted in (
        "My validation runs",          # the card heading
        "Awaiting submitter details",  # the draft status badge
        "Finish submission",           # the link in the Action column
        "<th>Action</th>",             # the column it sits in
    ):
        assert quoted in body, f"alma-certify tells submitters to look for {quoted!r}"


# --- how to produce a result -----------------------------------------------------------
#
# The front page tells a visitor how to run the suite. The dashboard is where somebody lands after
# signing in, and it named neither the command nor the two ways a result reaches the catalog, even
# though both of those already had pages of their own.


def _body(client, user):
    client.force_login(user)
    return client.get(reverse("accounts:dashboard")).content.decode()


def test_the_dashboard_says_how_to_run_the_suite(client, submitter):
    body = _body(client, submitter)

    assert "Run the certification suite" in body
    assert "sudo dnf -y install alma-certify &amp;&amp; sudo alma-certify run" in body


def test_it_names_both_ways_a_result_gets_here(client, submitter):
    """Which one applies is a fact about the machine, not a preference: a server with no route to
    this site cannot submit for itself. Offering only one path leaves that reader stuck."""
    body = _body(client, submitter)

    assert reverse("accounts:activate") in body
    assert reverse("results:upload") in body
    assert "alma-certify register" in body
    assert "alma-certify bundle" in body


def test_it_does_not_talk_about_needing_an_account(client, submitter):
    """The front page says so because its reader may not have one. This reader is signed in, and
    repeating it here would be noise."""
    body = " ".join(_body(client, submitter).split())

    assert "free account" not in body


def test_the_command_is_written_in_exactly_one_place(client, submitter):
    """Both pages include the same partial, so the placeholder becomes the real command once
    rather than leaving a public page and a signed-in page disagreeing about how to install it."""
    from pathlib import Path

    from django.conf import settings

    templates = Path(settings.BASE_DIR) / "templates"
    written = [
        str(path.relative_to(templates))
        for path in templates.rglob("*.html")
        if "install alma-certify" in path.read_text()
    ]

    assert written == ["core/_run_the_suite_command.html"]


def test_sidebar_offers_a_vendor_profile_entry_point(client, submitter):
    """Cataloguing a machine often needs its vendor to exist first, so the
    propose-a-vendor flow (with its claim toggle) is reachable from the workspace nav,
    not only from the dashboard body."""
    client.force_login(submitter)
    assert reverse("vendors:propose_new") in client.get(reverse("accounts:dashboard")).text


def test_benchmark_card_says_where_full_suite_benchmarks_live(client, submitter):
    """"My benchmark runs" reading empty after an `alma-certify run` looks like the
    benchmarks were lost; they are attached to the validation run instead."""
    client.force_login(submitter)
    text = client.get(reverse("accounts:dashboard")).text
    assert "submits its benchmarks" in text
    assert "My validation runs" in text


# --- notification preferences ----------------------------------------------------


def test_the_settings_page_offers_both_channels_independently(client):
    from django.urls import reverse

    from lumina.notifications.models import NotificationEndpoint
    from lumina.notifications.tests.helpers import dm_through

    endpoint = NotificationEndpoint.objects.create(
        name="chat", url="https://chat.invalid/hooks/y",
        kind=NotificationEndpoint.KIND_MATTERMOST, enabled=True,
    )
    dm_through(endpoint, "run.approved")
    user = User.objects.create_user("prefs", password="pw")
    client.force_login(user)

    body = client.get(reverse("accounts:settings")).content.decode()

    assert "Email me when something needs my attention" in body
    assert "Send me a Mattermost direct message" in body


def test_the_page_names_the_chat_endpoint_it_would_arrive_from(client):
    """So "Mattermost" is not an abstraction the reader has to trust: they see the
    workspace an administrator configured."""
    from django.urls import reverse

    from lumina.notifications.models import NotificationEndpoint
    from lumina.notifications.tests.helpers import dm_through

    endpoint = NotificationEndpoint.objects.create(
        name="AlmaLinux Mattermost", url="https://chat.invalid/hooks/x",
        kind=NotificationEndpoint.KIND_MATTERMOST, enabled=True,
    )
    dm_through(endpoint, "run.approved")
    user = User.objects.create_user("prefs2", password="pw")
    client.force_login(user)

    body = client.get(reverse("accounts:settings")).content.decode()

    assert "AlmaLinux Mattermost" in body


def test_staff_are_told_when_no_endpoint_can_deliver_a_direct_message(client):
    from django.urls import reverse

    staff = User.objects.create_user("prefs3", password="pw", is_staff=True)
    client.force_login(staff)

    body = client.get(reverse("accounts:settings")).content.decode()

    assert "No endpoint is set up to carry direct messages" in body


def test_saving_the_preferences_sticks(client):
    from django.urls import reverse

    from lumina.accounts.models import AccountSettings

    user = User.objects.create_user("prefs4", password="pw")
    client.force_login(user)

    client.post(reverse("accounts:settings"), {
        "chat_dm": "on",
    })

    saved = AccountSettings.objects.get(user=user)
    assert saved.chat_dm is True

    assert saved.email_notifications is False, "an unticked checkbox turns email off"
    assert saved.effective_chat_handle == saved.user.get_username()


def test_a_blank_handle_follows_the_username(client):
    from lumina.accounts.models import AccountSettings

    user = User.objects.create_user("prefs5", password="pw")
    row = AccountSettings.objects.create(user=user, chat_dm=True)

    assert row.effective_chat_handle == "prefs5"


def test_a_staff_reader_is_told_where_to_configure_chat(client):
    """The message used to say "once an administrator configures one" and stop there,
    which sends the administrator hunting: here they are usually the same person."""
    from django.urls import reverse

    staff = User.objects.create_user("prefs-staff", password="pw", is_staff=True)
    client.force_login(staff)

    body = client.get(reverse("accounts:settings")).content.decode()

    assert reverse("admin:notifications_notificationendpoint_add") in body
    assert "Mattermost incoming webhook" in body
    assert "to each person in the audience" in body


def test_an_end_user_is_not_offered_chat_that_cannot_deliver(client):
    """No endpoint and no admin access: the toggle would do nothing and the explanation
    would be something they cannot act on, so neither is shown."""
    from django.urls import reverse

    user = User.objects.create_user("prefs-plain", password="pw")
    client.force_login(user)

    body = client.get(reverse("accounts:settings")).content.decode()

    assert "direct message" not in body.lower()
    assert "/admin/notifications/" not in body
    # The email preference is unaffected: that one always works.
    assert "Email me when something needs my attention" in body


def test_an_end_user_is_offered_chat_once_an_endpoint_exists(client):
    from django.urls import reverse

    from lumina.notifications.models import NotificationEndpoint
    from lumina.notifications.tests.helpers import dm_through

    endpoint = NotificationEndpoint.objects.create(
        name="AlmaLinux Mattermost", url="https://chat.invalid/hooks/x",
        kind=NotificationEndpoint.KIND_MATTERMOST, enabled=True,
    )
    dm_through(endpoint, "run.approved")
    user = User.objects.create_user("prefs-plain2", password="pw")
    client.force_login(user)

    body = client.get(reverse("accounts:settings")).content.decode()

    assert "Send me a Mattermost direct message" in body
    assert "AlmaLinux Mattermost" in body
    assert "/admin/notifications/" not in body, "still not sent to the admin"


# --- what a new account starts with ----------------------------------------------


@pytest.mark.django_db
def test_both_channels_are_on_for_a_new_account():
    """Email and chat both, which is the site default rather than a constant in the model."""
    from lumina.accounts.models import AccountSettings, NotificationDefaults

    user = User.objects.create_user("fresh", email="fresh@example.com")

    prefs = AccountSettings.for_user(user)

    assert (prefs.email_notifications, prefs.chat_dm) == (True, True)
    assert NotificationDefaults.load().pk == 1


@pytest.mark.django_db
def test_a_new_account_follows_a_changed_default():
    from lumina.accounts.models import AccountSettings, NotificationDefaults

    defaults = NotificationDefaults.load()
    defaults.email_notifications = False
    defaults.save()

    prefs = AccountSettings.for_user(User.objects.create_user("later"))

    assert prefs.email_notifications is False


@pytest.mark.django_db
def test_the_defaults_are_one_row_however_they_are_saved():
    from lumina.accounts.models import NotificationDefaults

    NotificationDefaults(email_notifications=False).save()
    NotificationDefaults(chat_dm=False).save()

    assert NotificationDefaults.objects.count() == 1


@pytest.mark.django_db
def test_the_admin_offers_no_second_row_and_no_delete():
    from lumina.accounts.admin import NotificationDefaultsAdmin
    from lumina.accounts.models import NotificationDefaults

    admin = NotificationDefaultsAdmin(NotificationDefaults, None)

    assert admin.has_add_permission(None) is True
    NotificationDefaults.load()
    assert admin.has_add_permission(None) is False
    assert admin.has_delete_permission(None) is False


# --- the dashboard pages and searches on the server --------------------------------
#
# Every list was cut off at two hundred rows with nothing saying so, and the filter boxes matched
# only the rows that had survived the cut: a submitter hunting for their own run from last year
# was not told "not on this page", they were told it did not exist.


def _runs(owner, count, **kw):
    from lumina.results import ingest
    from lumina.results.tests import factories as f

    made = []
    for n in range(count):
        made.append(ingest.ingest_bundle(
            submitter=owner, source="api",
            bundle_file=f.as_upload(f.build_bundle(f.make_report(
                run_types=["validate"],
                results=[f.validate_result("validate.cpu.functional")],
                run_id=f"dddddddd-0000-0000-0000-{n:012d}",
                inventory=f.default_inventory() | {}, **kw,
            )))))
    return made


def test_a_long_run_list_is_cut_into_pages(client, submitter):
    from lumina.core.models import DisplayDefaults

    DisplayDefaults.objects.update_or_create(pk=1, defaults={"rows_per_page": 3})
    _runs(submitter, 5)
    client.force_login(submitter)

    section = client.get(reverse("accounts:dashboard")).context["sections"]["validation"]

    assert len(list(section.page)) == 3
    assert section.count == 5


def test_the_second_page_of_runs_is_reachable(client, submitter):
    from lumina.core.models import DisplayDefaults

    DisplayDefaults.objects.update_or_create(pk=1, defaults={"rows_per_page": 3})
    _runs(submitter, 5)
    client.force_login(submitter)

    section = client.get(
        reverse("accounts:dashboard"), {"page_validation": "2"}
    ).context["sections"]["validation"]

    assert len(list(section.page)) == 2


def test_paging_one_table_leaves_the_others_alone(client, submitter):
    """Eight lists on this page, and eight page numbers that have to stay apart."""
    client.force_login(submitter)

    sections = client.get(
        reverse("accounts:dashboard"), {"page_validation": "1", "page_systems": "1"}
    ).context["sections"]

    assert sections["validation"].param == "page_validation"
    assert sections["systems"].param == "page_systems"


def test_a_run_search_looks_past_the_first_page(client, submitter):
    """The row this is about is not on page one, which is exactly what the browser-side filter
    could not see."""
    from lumina.core.models import DisplayDefaults

    DisplayDefaults.objects.update_or_create(pk=1, defaults={"rows_per_page": 2})
    _runs(submitter, 4)
    client.force_login(submitter)

    section = client.get(
        reverse("accounts:dashboard"), {"q_validation": "PowerEdge"}
    ).context["sections"]["validation"]

    assert section.count == 4, "every matching run, not the two that were on screen"


def test_a_search_that_matches_nothing_says_so_rather_than_showing_everything(
    client, submitter,
):
    _runs(submitter, 2)
    client.force_login(submitter)

    response = client.get(reverse("accounts:dashboard"), {"q_validation": "nothing-like-this"})

    assert response.context["sections"]["validation"].count == 0
    assert "No validation run matches that" in response.content.decode()


def test_a_deep_link_into_the_archive_lands_on_the_archived_tab(client, submitter):
    """The panes are CSS-only radios, so a pager link into the archived half would otherwise land
    on the active one and page a list the reader cannot see."""
    client.force_login(submitter)

    body = client.get(
        reverse("accounts:dashboard"), {"page_validation_archived": "2"}
    ).content.decode()

    marker = 'id="validation-pane-archived"'
    assert 'class="pane-toggle pane-toggle-archived" checked' in body
    assert marker in body


def test_the_active_tab_is_the_default(client, submitter):
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).content.decode()

    assert 'id="validation-pane-active"\n         class="pane-toggle" checked' in body


def test_memberships_are_bounded_like_everything_else(client, submitter):
    from lumina.core.models import DisplayDefaults
    from lumina.vendors.models import Vendor, VendorMembership

    DisplayDefaults.objects.update_or_create(pk=1, defaults={"rows_per_page": 2})
    for n in range(4):
        VendorMembership.objects.create(
            user=submitter, vendor=Vendor.objects.create(name=f"Vendor {n}", slug=f"v{n}"),
            role=VendorMembership.ROLE_SUBMITTER)
    client.force_login(submitter)

    section = client.get(reverse("accounts:dashboard")).context["sections"]["memberships"]

    assert len(list(section.page)) == 2
    assert section.count == 4


def test_a_list_that_fits_on_one_page_has_no_search_box(client, submitter):
    """A box over three rows is clutter on a page that carries eight of them, and a reader can
    already see everything it would search."""
    _runs(submitter, 1)
    client.force_login(submitter)

    assert 'name="q_validation"' not in client.get(reverse("accounts:dashboard")).text


def test_a_list_longer_than_a_page_gets_one(client, submitter):
    from lumina.core.models import DisplayDefaults

    DisplayDefaults.objects.update_or_create(pk=1, defaults={"rows_per_page": 2})
    _runs(submitter, 3)
    client.force_login(submitter)

    assert 'name="q_validation"' in client.get(reverse("accounts:dashboard")).text


def test_the_box_stays_while_a_search_is_in_force(client, submitter):
    """It filters down to one row, and without the box there is no way to change or clear it."""
    _runs(submitter, 1)
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard"), {"q_validation": "PowerEdge"}).text

    assert 'name="q_validation"' in body and "Clear" in body


def test_the_pager_actually_renders_a_way_to_the_next_page(client, submitter):
    """The context can be paginated perfectly while the page offers no link to page two."""
    from lumina.core.models import DisplayDefaults

    DisplayDefaults.objects.update_or_create(pk=1, defaults={"rows_per_page": 2})
    _runs(submitter, 3)
    client.force_login(submitter)

    body = client.get(reverse("accounts:dashboard")).text

    assert "page_validation=2" in body
    assert "of 3 runs" in body, "the count that tells a reader the list did not stop at two"


def test_a_one_page_list_gets_no_pager(client, submitter):
    _runs(submitter, 1)
    client.force_login(submitter)

    assert "page_validation=" not in client.get(reverse("accounts:dashboard")).text
