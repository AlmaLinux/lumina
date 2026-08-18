"""POST /api/v1/results/ contract, read endpoints, and embargo visibility."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, User
from django.utils import timezone

from lumina.accounts.models import ApiToken
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release

pytestmark = pytest.mark.django_db

URL = "/api/v1/results/"


@pytest.fixture(autouse=True)
def _no_throttle(settings):
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            "results-ingest": None, "device-code": None, "device-token": None,
        },
    }


@pytest.fixture
def submitter():
    return User.objects.create_user("submitter", password="pw")


@pytest.fixture
def reviewer():
    user = User.objects.create_user("rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _submit_token(user) -> str:
    _, raw = ApiToken.issue(user=user, name="t", scopes=[ApiToken.SCOPE_SUBMIT])
    return raw


def _read_token(user) -> str:
    _, raw = ApiToken.issue(user=user, name="t", scopes=[ApiToken.SCOPE_READ])
    return raw


def _post_bundle(client, report=None, token=None, extra=None):
    data = {"bundle": f.as_upload(f.build_bundle(report or f.make_report()))}
    data.update(extra or {})
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return client.post(URL, data, **headers)


# --- ingest -------------------------------------------------------------------


def test_anonymous_post_is_401(client):
    resp = _post_bundle(client)
    assert resp.status_code == 401


def test_read_scope_token_is_403(client, submitter):
    resp = _post_bundle(client, token=_read_token(submitter))
    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_scope"


def test_submit_scope_token_creates_run(client, submitter):
    resp = _post_bundle(client, token=_submit_token(submitter))
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft"   # the default bundle is a validate run
    assert "/results/runs/" in body["web_url"]
    run = TestRun.objects.get(uuid=body["uuid"])
    assert run.submitter == submitter
    assert run.source == "api"


def test_ingest_response_reports_rule_excluded_components(client, submitter):
    """The submission notice: a device a rule unticks (here a BMC display adapter, caught by the
    categorical rule) is reported in the upload response so the submitter is told at upload, not
    left to discover it in review."""
    inv = f.default_inventory()
    inv["summary"]["gpus"] = []
    inv["summary"]["nics"] = []
    inv["summary"]["pci_devices"] = [{
        "pci": "0c:00.0", "class": "VGA compatible controller [0300]", "class_id": "0300",
        "pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                    "device": "ASPEED Graphics Family [2000]"}, "driver": "ast",
    }]
    report = f.make_report(
        run_types=["validate"], inventory=inv,
        results=[f.validate_result("validate.cpu.functional")],
    )
    resp = _post_bundle(client, report=report, token=_submit_token(submitter))

    assert resp.status_code == 201
    excluded = resp.json()["excluded_components"]
    assert len(excluded) == 1
    assert excluded[0]["component"] == "ASPEED Graphics Family"
    assert excluded[0]["kind"] == "GPU"
    assert "management display" in excluded[0]["reason"]


def test_a_run_with_no_excluded_components_reports_an_empty_list(client, submitter):
    body = _post_bundle(client, token=_submit_token(submitter)).json()
    assert body["excluded_components"] == []


def test_a_non_almalinux_bundle_is_accepted_and_quarantined(client, submitter):
    """The documented contract for a run from another distribution.

    Accepted rather than refused, so the attempt is visible to a reviewer instead
    of silently bounced - but ``status`` says so, and it is not in any public
    queryset. See docs/api.md, "Runs not performed on AlmaLinux".
    """
    resp = _post_bundle(
        client, report=f.make_report(os_id="rocky"), token=_submit_token(submitter),
    )

    assert resp.status_code == 201
    assert resp.json()["status"] == TestRun.STATUS_QUARANTINED
    run = TestRun.objects.get(uuid=resp.json()["uuid"])
    assert run.alma_release is None
    assert run not in TestRun.objects.public()


def test_a_quarantined_run_is_not_in_the_public_list(client, submitter):
    _post_bundle(
        client, report=f.make_report(os_id="rocky"), token=_submit_token(submitter),
    )

    body = client.get(URL).json()

    assert body["count"] == 0, body


def test_missing_bundle_field(client, submitter):
    resp = client.post(URL, {}, HTTP_AUTHORIZATION=f"Bearer {_submit_token(submitter)}")
    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_bundle"


def test_bad_schema_maps_to_400_with_code(client, submitter):
    resp = _post_bundle(
        client, f.make_report(schema_version="9.9"), token=_submit_token(submitter)
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unsupported_schema"


def test_oversize_maps_to_413(client, submitter, settings):
    settings.LUMINA_BUNDLE_MAX_BYTES = 10
    resp = _post_bundle(client, token=_submit_token(submitter))
    assert resp.status_code == 413


def test_exact_replay_is_200_duplicate(client, submitter):
    report = f.make_report()
    token = _submit_token(submitter)
    bundle = f.build_bundle(report)
    first = client.post(URL, {"bundle": f.as_upload(bundle)},
                        HTTP_AUTHORIZATION=f"Bearer {token}")
    assert first.status_code == 201
    bundle.seek(0)
    second = client.post(URL, {"bundle": f.as_upload(bundle)},
                         HTTP_AUTHORIZATION=f"Bearer {token}")
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["uuid"] == first.json()["uuid"]


def test_same_uuid_different_content_is_409(client, submitter):
    token = _submit_token(submitter)
    run_id = "11111111-2222-3333-4444-555555555555"
    assert _post_bundle(client, f.make_report(run_id=run_id), token=token).status_code == 201
    resp = _post_bundle(
        client,
        f.make_report(run_id=run_id, results=[f.validate_result("validate.x")]),
        token=token,
    )
    assert resp.status_code == 409


def test_form_fields_override_report_embargo(client, submitter):
    resp = _post_bundle(
        client,
        token=_submit_token(submitter),
        extra={"pre_release": "true", "publish_after": "2026-12-01"},
    )
    run = TestRun.objects.get(uuid=resp.json()["uuid"])
    assert run.pre_release is True
    assert str(run.publish_requested_date) == "2026-12-01"


# --- read side + embargo visibility -------------------------------------------


def _public_run(submitter, reviewer, report=None) -> TestRun:
    run = ingest.ingest_bundle(
        submitter=submitter,
        bundle_file=f.as_upload(f.build_bundle(report or f.make_report())),
        source="api",
    )
    services.approve_run(release(run), by=reviewer)
    run.refresh_from_db()
    return run


def test_list_shows_only_public_runs(client, submitter, reviewer):
    public = _public_run(submitter, reviewer)
    pending = ingest.ingest_bundle(
        submitter=submitter, bundle_file=f.as_upload(f.build_bundle(f.make_report())),
        source="api",
    )
    resp = client.get(URL)
    uuids = [row["uuid"] for row in resp.json()["results"]]
    assert str(public.uuid) in uuids
    assert str(pending.uuid) not in uuids


def test_embargoed_run_is_completely_invisible(client, submitter, reviewer):
    future = (timezone.localdate() + timedelta(days=30)).isoformat()
    run = ingest.ingest_bundle(
        submitter=submitter,
        bundle_file=f.as_upload(
            f.build_bundle(f.make_report(pre_release=True, publish_after=future))
        ),
        source="api",
    )
    services.approve_run(release(run), by=reviewer)

    # absent from the list - no placeholder
    resp = client.get(URL)
    assert str(run.uuid) not in [row["uuid"] for row in resp.json()["results"]]
    # anonymous detail is a plain 404
    assert client.get(f"{URL}{run.uuid}/").status_code == 404
    # the HTML page 404s for anonymous visitors too
    assert client.get(f"/results/runs/{run.uuid}/").status_code == 404


def test_embargoed_run_visible_to_submitter_and_reviewer(
    client, submitter, reviewer
):
    future = (timezone.localdate() + timedelta(days=30)).isoformat()
    run = ingest.ingest_bundle(
        submitter=submitter,
        bundle_file=f.as_upload(
            f.build_bundle(f.make_report(pre_release=True, publish_after=future))
        ),
        source="api",
    )
    services.approve_run(release(run), by=reviewer)

    client.force_login(submitter)
    assert client.get(f"/results/runs/{run.uuid}/").status_code == 200
    client.force_login(reviewer)
    assert client.get(f"/results/runs/{run.uuid}/").status_code == 200
    other = User.objects.create_user("bystander", password="pw")
    client.force_login(other)
    assert client.get(f"/results/runs/{run.uuid}/").status_code == 404


def test_run_detail_includes_results_and_benchmarks(client, submitter, reviewer):
    report = f.make_report(
        run_types=["validate", "benchmark"],
        results=[f.validate_result("validate.cpu.functional"), f.benchmark_result()],
    )
    run = _public_run(submitter, reviewer, report)
    body = client.get(f"{URL}{run.uuid}/").json()
    assert body["verdict"] is True
    assert len(body["results"]) == 2
    assert len(body["benchmarks"]) == 1
    assert body["cpu_model"] == "Intel(R) Xeon(R) Gold 6430"


def test_leaderboard_endpoint(client, submitter, reviewer):
    report = f.make_report(run_types=["benchmark"], results=[f.benchmark_result()])
    _public_run(submitter, reviewer, report)
    resp = client.get("/api/v1/benchmarks/bench.cpu.sysbench-multi/leaderboard/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metric"] == "events_per_sec"
    assert len(body["results"]) == 1
    assert body["results"][0]["value"] == "41230.500000"


def test_metrics_endpoint_gpu_facet_is_keyed_by_pci_id(client, submitter, reviewer):
    """The GPU facet is the one place value and label differ: a card is filtered by its PCI group
    key but shown by name, so the facet is {value, label} pairs. Every other facet stays a bare
    string list (there value == label). Pinned because ?gpu= takes the value, and a client that read
    it as a plain string would send back something that matches no card."""
    inv = f.default_inventory()
    inv["summary"]["gpus"] = [{
        "pci": "0000:00:02.0",
        "pci_ids": {"vendor": "Intel Corporation [8086]",
                    "device": "Arrow Lake-U [Intel Graphics] [7d55]"},
        "driver": "i915",
    }]
    report = f.make_report(
        run_types=["benchmark"], inventory=inv,
        results=[f.benchmark_result(
            test_id="bench.gpu.clpeak", category="gpu",
            metrics=[{"name": "vulkan_single_precision_compute", "value": 1000.0,
                      "unit": "GFLOPS", "direction": "higher_is_better",
                      "device": "Intel Graphics (ARL)", "primary": True}],
        )],
    )
    _public_run(submitter, reviewer, report)

    body = client.get("/api/v1/benchmarks/bench.gpu.clpeak/metrics/").json()

    assert body["gpu"] == [{"value": "8086:7d55", "label": "Intel Graphics (ARL)"}]
    # A sibling facet is still a bare-string list, so the gpu shape is a deliberate exception.
    assert all(isinstance(v, str) for v in body["gpu_driver"])


def test_benchmark_catalog_lists_every_defined_benchmark_with_public_counts(
    client, submitter, reviewer
):
    """The catalog is what lumina defines, not what was submitted: every active benchmark is listed,
    a retired one never is, and the run count is public-only - a pending run adds nothing to it."""
    report = f.make_report(run_types=["benchmark"], results=[f.benchmark_result()])
    _public_run(submitter, reviewer, report)
    # a pending benchmark run does not add to the count
    ingest.ingest_bundle(
        submitter=submitter,
        bundle_file=f.as_upload(
            f.build_bundle(
                f.make_report(run_types=["benchmark"],
                              results=[f.benchmark_result("bench.mem.bandwidth",
                                                          category="memory")])
            )
        ),
        source="api",
    )
    by_id = {row["benchmark_id"]: row for row in client.get("/api/v1/benchmarks/").json()}

    assert by_id["bench.cpu.sysbench-multi"]["runs"] == 1, "the public run counts"
    assert by_id["bench.mem.bandwidth"]["runs"] == 0, "defined and listed, but the run is pending"
    assert "bench.sched.hackbench" not in by_id, "a retired benchmark is never listed"


# --- host binding on ingest ----------------------------------------------------


def _bound_token(user, hostname):
    from lumina.accounts.models import ApiToken

    _, raw = ApiToken.issue(
        user=user, name="cli", scopes=["submit"], hostname=hostname
    )
    return raw


def test_bound_token_accepts_results_from_its_own_host(client, submitter):
    report = f.make_report()
    report["run"]["hostname"] = "sut-1.lab"
    raw = _bound_token(submitter, "sut-1.lab")

    resp = client.post(
        "/api/v1/results/", {"bundle": f.as_upload(f.build_bundle(report))},
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert resp.status_code == 201


def test_bound_token_refuses_results_from_another_host(client, submitter):
    """A leaked token cannot be used to post from somewhere else without
    also forging the hostname inside the report."""
    report = f.make_report()
    report["run"]["hostname"] = "somewhere-else.lab"
    raw = _bound_token(submitter, "sut-1.lab")

    resp = client.post(
        "/api/v1/results/", {"bundle": f.as_upload(f.build_bundle(report))},
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "hostname_mismatch"
    # the message says what to do about it
    assert "sut-1.lab" in body["detail"]
    assert "somewhere-else.lab" in body["detail"]
    assert "register" in body["detail"]


def test_unbound_token_accepts_any_host(client, submitter):
    """Admin-issued tokens carry no binding, so they keep working."""
    from lumina.accounts.models import ApiToken

    report = f.make_report()
    report["run"]["hostname"] = "any-host.lab"
    _, raw = ApiToken.issue(user=submitter, name="admin", scopes=["submit"])

    resp = client.post(
        "/api/v1/results/", {"bundle": f.as_upload(f.build_bundle(report))},
        HTTP_AUTHORIZATION=f"Bearer {raw}",
    )
    assert resp.status_code == 201


# --- token liveness -------------------------------------------------------------
#
# Reported from real use: the CLI held a token issued before the server's database was
# rebuilt. Its stored ``expires_at`` was hours away, so the pre-run check passed, the
# validation run completed, and the upload failed at the end with the whole run spent.
#
# ``GET /api/v1/token`` exists so the CLI can ask instead of guessing. The client half is
# in the suite's ``tests/test_token_lifecycle.py``.


@pytest.mark.django_db
def test_a_live_token_reports_its_scopes(client):
    from lumina.accounts.models import ApiToken

    user = User.objects.create_user("probe")
    _token, raw = ApiToken.issue(
        user=user, name="cli", scopes=[ApiToken.SCOPE_SUBMIT], hostname="box",
    )

    resp = client.get("/api/v1/token", HTTP_AUTHORIZATION=f"Bearer {raw}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["username"] == "probe"
    assert body["scopes"] == ["submit"]
    assert body["hostname"] == "box"


@pytest.mark.django_db
def test_a_read_only_token_may_still_ask(client):
    """Its whole job is answering "is this credential real", so gating it behind the
    submit scope would leave a read token unable to discover that it is a read token -
    which is precisely what the CLI needs to warn about before a run."""
    from lumina.accounts.models import ApiToken

    user = User.objects.create_user("reader")
    _token, raw = ApiToken.issue(user=user, name="cli", scopes=[ApiToken.SCOPE_READ])

    resp = client.get("/api/v1/token", HTTP_AUTHORIZATION=f"Bearer {raw}")

    assert resp.status_code == 200
    assert resp.json()["scopes"] == ["read"]


@pytest.mark.django_db
def test_an_unknown_token_is_401(client):
    """401 and not 403, so the client can tell "your credential is no good" from
    "forbidden". Getting this needs ApiTokenAuthentication to be the *only* authenticator
    on the view: DRF takes the status from the first authenticator's
    ``authenticate_header``, and with the project's default SessionAuthentication first
    this answered 403."""
    resp = client.get("/api/v1/token", HTTP_AUTHORIZATION="Bearer not-a-real-token")

    assert resp.status_code == 401


@pytest.mark.django_db
def test_a_revoked_token_is_401(client):
    from django.utils import timezone

    from lumina.accounts.models import ApiToken

    user = User.objects.create_user("revoked-probe")
    token, raw = ApiToken.issue(user=user, name="cli", scopes=[ApiToken.SCOPE_SUBMIT])
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])

    assert client.get(
        "/api/v1/token", HTTP_AUTHORIZATION=f"Bearer {raw}"
    ).status_code == 401


@pytest.mark.django_db
def test_no_credential_at_all_is_401_not_a_cheerful_yes(client):
    """The first version of this endpoint answered 200 with ``{"valid": true}`` for the
    AnonymousUser, because the project default is ``IsAuthenticatedOrReadOnly`` and this
    is a GET. A CLI holding no token would have been told its token was fine."""
    resp = client.get("/api/v1/token")

    assert resp.status_code == 401


@pytest.mark.django_db
def test_a_session_cannot_stand_in_for_a_token(client):
    """Token auth only. A browser session answering yes would make the endpoint mean
    "somebody is logged in" rather than "this bearer token works"."""
    user = User.objects.create_user("sessioned", password="pw")
    client.force_login(user)

    assert client.get("/api/v1/token").status_code == 401
