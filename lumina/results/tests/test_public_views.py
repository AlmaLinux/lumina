"""Public pages: upload form, leaderboards, stats, feeds, listing cert section."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse
from django.utils import timezone

from lumina.hardware.models import System
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def submitter():
    return User.objects.create_user("submitter", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _ingested(submitter, report=None):
    return ingest.ingest_bundle(
        submitter=submitter,
        bundle_file=f.as_upload(f.build_bundle(report or f.make_report())),
        source="api",
    )


def _published(submitter, reviewer, report=None):
    run = _ingested(submitter, report)
    services.approve_run(release(run), by=reviewer)
    run.refresh_from_db()
    return run


# --- web upload ---------------------------------------------------------------


def test_upload_requires_login(client):
    assert client.get(reverse("results:upload")).status_code == 302


def test_upload_happy_path(client, submitter):
    client.force_login(submitter)
    bundle = f.as_upload(f.build_bundle(f.make_report()))
    resp = client.post(reverse("results:upload"), {"bundle": bundle})
    assert resp.status_code == 302
    run = TestRun.objects.get()
    assert run.source == "web_upload"
    assert run.submitter == submitter


def test_upload_rejects_bad_bundle_with_form_error(client, submitter):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client.force_login(submitter)
    junk = SimpleUploadedFile("x.tar.zst", b"garbage", "application/zstd")
    resp = client.post(reverse("results:upload"), {"bundle": junk})
    assert resp.status_code == 200
    assert "not zstd- or gzip-compressed" in resp.text


def test_upload_publish_date_requires_pre_release_flag(client, submitter):
    client.force_login(submitter)
    bundle = f.as_upload(f.build_bundle(f.make_report()))
    resp = client.post(
        reverse("results:upload"),
        {"bundle": bundle, "publish_after": "2027-01-01"},
    )
    assert resp.status_code == 200  # form error, not a crash
    assert TestRun.objects.count() == 0


# --- leaderboard + stats pages -------------------------------------------------


def test_leaderboard_page_orders_and_filters(client, submitter, reviewer):
    fast = f.make_report(run_types=["benchmark"], results=[
        f.benchmark_result(metrics=[{"name": "events_per_sec", "value": 50000,
                                     "unit": "events/s",
                                     "direction": "higher_is_better", "primary": True}])
    ])
    slow_inventory = f.default_inventory()
    slow_inventory["summary"]["cpus"][0]["model"] = "Slow CPU 1000"
    slow = f.make_report(run_types=["benchmark"], inventory=slow_inventory, results=[
        f.benchmark_result(metrics=[{"name": "events_per_sec", "value": 100,
                                     "unit": "events/s",
                                     "direction": "higher_is_better", "primary": True}])
    ])
    _published(submitter, reviewer, fast)
    _published(submitter, reviewer, slow)

    url = reverse("benchmarks:leaderboard", args=["bench.cpu.sysbench-multi"])
    resp = client.get(url)
    assert resp.status_code == 200
    # rank within the table itself (the facet sidebar also names the CPUs)
    table = resp.text.split('id="leaderboard"', 1)[1]
    assert table.index("50000") < table.index("Slow CPU 1000")

    filtered = client.get(url, {"cpu": "Slow CPU 1000"})
    filtered_table = filtered.text.split('id="leaderboard"', 1)[1]
    assert "50000" not in filtered_table


def test_leaderboard_404_when_no_public_results(client):
    url = reverse("benchmarks:leaderboard", args=["bench.no.such"])
    assert client.get(url).status_code == 404


def test_stats_page_counts_only_public_runs(client, submitter, reviewer):
    _published(submitter, reviewer)          # public
    _ingested(submitter)                     # pending - must not count
    resp = client.get(reverse("results:stats"))
    assert resp.status_code == 200
    assert "across 1 distinct system" in resp.text


# --- feeds ---------------------------------------------------------------------


def test_validation_feed_lists_published_only(client, submitter, reviewer):
    report = f.make_report(
        run_types=["validate"], results=[f.validate_result("validate.cpu.functional")]
    )
    run = _published(submitter, reviewer, report)
    _ingested(submitter, f.make_report(
        run_types=["validate"], results=[f.validate_result("validate.cpu.functional")]
    ))
    resp = client.get(reverse("results:validations_feed"))
    assert resp.status_code == 200
    assert str(run.uuid) in resp.text
    assert resp.text.count("<entry>") == 1
    assert "validated" in resp.text


def test_benchmark_feed_embargo_safe(client, submitter, reviewer):
    future = (timezone.localdate() + timedelta(days=30)).isoformat()
    embargoed = _ingested(submitter, f.make_report(
        run_types=["benchmark"], results=[f.benchmark_result()],
        pre_release=True, publish_after=future,
    ))
    services.approve_run(release(embargoed), by=reviewer)
    resp = client.get(reverse("results:benchmarks_feed"))
    assert str(embargoed.uuid) not in resp.text
    assert resp.text.count("<entry>") == 0


# --- listing certification section ---------------------------------------------


def test_listing_page_shows_certification_results(client, submitter, reviewer):
    vendor = Vendor.objects.create(name="ACME", verified=True, published=True)
    system = System.objects.create(
        name="R760", vendor=vendor, owner_vendor=vendor, published=True
    )
    report = f.make_report(
        run_types=["validate"], results=[f.validate_result("validate.cpu.functional")]
    )
    run = _ingested(submitter, report)
    run.listing_system = system
    run.save()
    services.approve_run(release(run), by=reviewer)

    resp = client.get(reverse("hardware:detail", args=[system.slug]))
    assert resp.status_code == 200
    assert "Certification results" in resp.text
    assert "PASS" in resp.text
    system.refresh_from_db()
    assert system.attestation_count == 1


def test_embargo_lifecycle_end_to_end(client, submitter, reviewer):
    """Approve embargoed → invisible → timer publishes → visible."""
    tomorrow = timezone.localdate() + timedelta(days=1)
    run = _ingested(submitter, f.make_report(
        run_types=["validate"],
        results=[f.validate_result("validate.cpu.functional")],
        pre_release=True, publish_after=tomorrow.isoformat(),
    ))
    services.approve_run(release(run), by=reviewer)

    feed_url = reverse("results:validations_feed")
    assert str(run.uuid) not in client.get(feed_url).text

    services.publish_due_runs(today=tomorrow)
    assert str(run.uuid) in client.get(feed_url).text
