"""A run can be a claim about one component instead of about a machine.

Asked for so a GPU can be certified on its own: an NVIDIA L40S passed through to a cloud instance
is the card the vendor wants certified, and the instance around it is a rented hypervisor guest
nobody should certify anything about. CPUs are the same, being handed to the guest directly.

The safety property is one sentence, and these tests exist to hold it: **a scoped run certifies
components of the kinds it claims, and never a System.** It is enforced in ``scoped_listings``,
which every path into the catalog goes through, rather than by being careful at each call site.

What a scoped run still does is report everything it collected. A GPU-scoped run in a guest sees a
CPU, a NIC, and whatever the hypervisor calls its chassis, and recording all of that is right: it is
context a reviewer wants. Certifying it would be a claim nobody made, on the strength of tests that
were never run for it.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from lumina.hardware.models import CommunityAttestation, ComponentKind, System
from lumina.releases.models import AlmaLinuxRelease
from lumina.results import ingest, services
from lumina.results.models import TestRun
from lumina.results.tests import factories as f
from lumina.results.tests.helpers import release as _ready
from lumina.vendors.models import Vendor, VendorMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def releases():
    AlmaLinuxRelease.objects.get_or_create(major=9, defaults={"supported": True})


@pytest.fixture
def nvidia():
    vendor, _ = Vendor.objects.get_or_create(
        name="NVIDIA", defaults={"published": True},
    )
    vendor.verified = True
    vendor.save(update_fields=["verified"])
    return vendor


@pytest.fixture
def reviewer():
    user = User.objects.create_user("scope-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


@pytest.fixture
def gpu_engineer(nvidia):
    user = User.objects.create_user("scope-nvidia", password="pw")
    VendorMembership.objects.create(
        user=user, vendor=nvidia, role=VendorMembership.ROLE_SUBMITTER,
    )
    return user


# A gating GPU result, which is what makes a GPU claim certifiable at all. Not decoration: without
# one ``certifies`` refuses, and that refusal is the second half of the safety property. See
# ``unevidenced_claims``, and the four NVIDIA tests in the suite that produce results like this.
GPU_EVIDENCE = ("validate.gpu.cuda-vectoradd", "gpu")


def _bundle(scope=None, results=None, **report_kw):
    report = f.make_report(
        run_types=["validate"],
        results=results if results is not None else [
            f.validate_result("validate.cpu.functional"),
            f.validate_result(GPU_EVIDENCE[0], category=GPU_EVIDENCE[1]),
        ],
        **report_kw,
    )
    if scope is not None:
        report["run"]["claim_scope"] = scope
    return f.as_upload(f.build_bundle(report))


def _run(submitter, scope=None, **report_kw):
    return ingest.ingest_bundle(
        submitter=submitter, source="api", bundle_file=_bundle(scope, **report_kw),
    )


def _attested():
    out = {}
    for a in CommunityAttestation.objects.select_related("version"):
        listing = a.version.listing_system or a.version.listing_component
        out[str(listing)] = a.level
    return out


# --- the field ------------------------------------------------------------------------


def test_a_report_without_a_scope_is_a_whole_machine_run(gpu_engineer):
    """Which is every bundle written before the field existed, and most written after."""
    run = _run(gpu_engineer)

    assert run.claim_scope == []
    assert run.is_scoped is False


def test_a_scope_is_recorded(gpu_engineer):
    run = _run(gpu_engineer, scope=["gpu"])

    assert run.claim_scope == ["gpu"]
    assert run.is_scoped is True
    assert run.scope_labels == ["GPU"]


def test_a_scope_is_deduplicated_and_ordered(gpu_engineer):
    """So two reports claiming the same thing in a different order store the same value and render
    the same sentence."""
    run = _run(gpu_engineer, scope=["gpu", "cpu", "gpu"])

    assert run.claim_scope == ["cpu", "gpu"]


def test_a_scope_nobody_can_interpret_is_refused_at_the_door(gpu_engineer):
    """A claim of unknown size is worse than no claim: stored, it would be guessed at later."""
    with pytest.raises(ingest.InvalidReport) as caught:
        ingest.ingest_bundle(
            submitter=gpu_engineer, source="api", bundle_file=_bundle(["teleporter"]),
        )

    assert "teleporter" in str(caught.value)


def test_a_scope_that_is_not_a_list_is_refused(gpu_engineer):
    with pytest.raises(ingest.InvalidReport):
        ingest.ingest_bundle(
            submitter=gpu_engineer, source="api", bundle_file=_bundle("gpu"),
        )


# --- the safety property --------------------------------------------------------------


def test_a_scoped_run_certifies_no_system(gpu_engineer, reviewer):
    """The whole point. The machine around a passed-through card is a rented guest, and the next
    boot is different hardware."""
    run = _run(gpu_engineer, scope=["gpu"])
    system = System.objects.create(
        vendor=Vendor.objects.get_or_create(name="Dell Inc.")[0], name="PowerEdge R760",
    )
    run.listing_system = system
    run.save(update_fields=["listing_system"])

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    assert str(system) not in _attested()
    assert system.attestation_count == 0


def test_a_scoped_run_ties_only_the_kinds_it_claims(gpu_engineer, reviewer):
    """A GPU-scoped run in a guest still *reports* a CPU, a board, and a NIC. Reporting them is
    right: they are context for a reader. Tying them would make them things this run proved
    something about, and it proved nothing about them."""
    run = _run(gpu_engineer, scope=["gpu"])
    reported = {gpu.get("model") for gpu in run.inventory["summary"].get("gpus", [])}
    assert reported, "the premise: the inventory still has the whole machine in it"
    assert run.cpu_model, "including a CPU nobody claimed"

    services.ensure_component_ties(run)

    assert {c.kind for c in run.listing_components.all()} == {ComponentKind.gpu.value}

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    # On the kind, not on the name. "NVIDIA L40S" is a GPU and reads like anything.
    certified_kinds = {
        a.version.listing_component.kind
        for a in CommunityAttestation.objects.select_related("version__listing_component")
        if a.version.listing_component is not None
    }
    assert certified_kinds == {ComponentKind.gpu.value}, certified_kinds
    assert not CommunityAttestation.objects.filter(
        version__listing_system__isnull=False,
    ).exists()


def test_the_rule_is_in_one_place(gpu_engineer):
    """``scoped_listings`` is what every path into the catalog asks. Three copies of a rule about
    what a run may certify is two copies too many, and a future caller cannot forget this one."""
    run = _run(gpu_engineer, scope=["gpu"])
    services.ensure_component_ties(run)
    system = System.objects.create(
        vendor=Vendor.objects.get_or_create(name="Dell Inc.")[0], name="PowerEdge R760",
    )
    run.listing_system = system
    run.save(update_fields=["listing_system"])

    listings = services.scoped_listings(run)

    assert system not in listings
    assert listings, "it must still certify something, or the run is pointless"
    assert all(getattr(o, "kind", None) == ComponentKind.gpu.value for o in listings)


def test_compatibility_is_recorded_only_for_the_claimed_parts(gpu_engineer, reviewer):
    """A compatibility row saying "this server works on AlmaLinux 9" is a certification claim in
    the catalog's own table."""
    from lumina.hardware.models import ListingVersion

    run = _run(gpu_engineer, scope=["gpu"])
    system = System.objects.create(
        vendor=Vendor.objects.get_or_create(name="Dell Inc.")[0], name="PowerEdge R760",
    )
    run.listing_system = system
    run.save(update_fields=["listing_system"])

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    assert not ListingVersion.objects.filter(listing_system=system).exists()
    assert ListingVersion.objects.filter(listing_component__isnull=False).exists()


def test_an_architecture_facet_is_not_written_onto_the_host(gpu_engineer, reviewer):
    """An architecture facet is a fact published on a catalog page."""
    from lumina.hardware.models import ListingCategoryValue

    run = _run(gpu_engineer, scope=["gpu"])
    system = System.objects.create(
        vendor=Vendor.objects.get_or_create(name="Dell Inc.")[0], name="PowerEdge R760",
    )
    run.listing_system = system
    run.save(update_fields=["listing_system"])

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    assert not ListingCategoryValue.objects.filter(listing_system=system).exists()


# --- what a scoped run may still earn --------------------------------------------------


def test_the_cards_own_vendor_still_earns_the_vendor_tier(gpu_engineer, reviewer):
    """The use case, not an edge case: NVIDIA validating an L40S in a cloud instance is exactly
    the run this feature exists for, and it has to be worth something."""
    run = _run(gpu_engineer, scope=["gpu"])

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    attested = _attested()
    assert attested, "a scoped run that certifies nothing is pointless"


def test_a_whole_machine_run_is_unaffected(gpu_engineer, reviewer):
    """The regression that matters most. Every existing run has an empty scope, so nothing about
    the ordinary path may change."""
    run = _run(gpu_engineer)
    run.listing_proposal = {"vendor_name": "Dell Inc.", "name": "PowerEdge R760",
                            "machine_kind": "prebuilt"}
    run.save(update_fields=["listing_proposal"])

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    assert run.listing_system is not None
    assert str(run.listing_system) in _attested()


def test_an_existing_run_in_the_database_reads_as_unscoped(gpu_engineer):
    """The migration adds a column with a list default, so a row written before it existed has an
    empty scope and keeps its old meaning."""
    run = _run(gpu_engineer)
    TestRun.objects.filter(pk=run.pk).update(claim_scope=[])

    run.refresh_from_db()
    assert run.is_scoped is False


# --- the host paths, which the standing guard alone did not close ----------------------
#
# Found by reproducing an EC2 GPU run through the real ingest and approval path rather than through
# a factory. The guard in ``scoped_listings`` held the standing line: nothing was published or
# attested. Everything upstream of it leaked, and the second tenant is why it mattered.


def _cloud_bundle(scope, run_id=None):
    report = f.make_report(
        run_types=["validate"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result(GPU_EVIDENCE[0], category=GPU_EVIDENCE[1]),
        ],
        run_id=run_id,
    )
    report["run"]["claim_scope"] = scope
    summary = report["inventory"]["summary"]
    summary["system"] = {"vendor": "Amazon EC2", "product": "m5.large", "serial": "ec2abc"}
    summary["baseboard"] = {"vendor": "Amazon EC2", "product": "0.1"}
    return f.as_upload(f.build_bundle(report))


def test_a_scoped_cloud_run_certifies_nothing_about_its_host(gpu_engineer, reviewer):
    """The invariant, end to end, against the shape that broke it.

    An instance type is not a machine: it is shared by every tenant, the next boot is different
    silicon, and nobody can certify it by validating a card that happened to be passed through.
    """
    from lumina.results.models import ReportedIdentityAlias

    run = ingest.ingest_bundle(
        submitter=gpu_engineer, source="api", bundle_file=_cloud_bundle(["gpu"]),
    )

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    run.refresh_from_db()
    assert System.objects.count() == 0, "a rented instance type became a machine listing"
    assert run.listing_system_id is None
    assert not Vendor.objects.filter(name__icontains="Amazon").exists()
    assert {c.kind for c in run.listing_components.all()} == {ComponentKind.gpu.value}
    assert not ReportedIdentityAlias.objects.exists(), (
        "the alias table is keyed on machine strings and is global: one row would teach every "
        "future run of this instance type to attach itself here"
    )


def test_the_next_tenant_is_not_attached_to_the_first_ones_instance(gpu_engineer, reviewer):
    """The sharpest of the leaks. A second GPU-scoped run of the same instance type matched the
    System the first one created and arrived at review already bound to it with nothing
    outstanding, so a reviewer was asked to approve a card against somebody else's rental."""
    other = User.objects.create_user("scope-other", password="pw")
    first = ingest.ingest_bundle(
        submitter=gpu_engineer, source="api", bundle_file=_cloud_bundle(["gpu"]),
    )
    services.approve_run(_ready(first, gpu_engineer), by=reviewer)

    second = ingest.ingest_bundle(
        submitter=other, source="api",
        bundle_file=_cloud_bundle(["gpu"], "bbbbbbbb-0000-0000-0000-000000000009"),
    )

    assert second.listing_system_id is None


def test_a_reviewer_cannot_attach_a_machine_to_a_scoped_run(gpu_engineer, reviewer):
    """Refused rather than ignored: a reviewer who picked one made a decision and is owed an
    answer about it. The components half stays open, which is what a scoped run actually needs."""
    run = _run(gpu_engineer, scope=["gpu"])
    system = System.objects.create(
        vendor=Vendor.objects.get_or_create(name="Dell Inc.")[0], name="PowerEdge R760",
    )

    with pytest.raises(services.ReviewError) as caught:
        services.assign_listing(run, system=system, components=None, by=reviewer)

    assert "GPU" in str(caught.value)
    run.refresh_from_db()
    assert run.listing_system_id is None


def test_a_scoped_run_that_identifies_nothing_is_refused(gpu_engineer, reviewer):
    """"Nothing in this run identifies the machine" was the wrong diagnosis here: the DMI
    identifies something and that something is a rental SKU. The right answer names the claim."""
    report = f.make_report(
        run_types=["validate"], results=[f.validate_result("validate.cpu.functional")],
    )
    report["run"]["claim_scope"] = ["storage"]
    report["inventory"]["summary"]["gpus"] = []
    run = ingest.ingest_bundle(
        submitter=gpu_engineer, source="api", bundle_file=f.as_upload(f.build_bundle(report)),
    )

    with pytest.raises(services.ReviewError) as caught:
        services.create_listings_from_run(run, by=reviewer)

    assert "nothing to certify" in str(caught.value)


def test_a_scoped_run_does_not_attach_to_a_machine_already_in_the_catalog(
    gpu_engineer, reviewer,
):
    """The case that makes the auto-link guard load-bearing, and it is not a cloud one.

    Validating a card inside a PowerEdge R760 that is already certified must add nothing to the
    R760. The run was not a machine validation: most of the suite did not run, and the operator
    said as much by scoping it. Without the guard the reported DMI matches the existing listing at
    ingest and the run arrives attached, which is the same wrong claim as the cloud case wearing a
    respectable name.
    """
    dell = Vendor.objects.get_or_create(name="Dell Inc.", defaults={"published": True})[0]
    listed = System.objects.create(vendor=dell, name="PowerEdge R760", published=True)
    before = listed.attestation_count

    # The factory's inventory reports exactly this machine, so an unscoped run of it would link.
    unscoped = _run(gpu_engineer)
    assert unscoped.listing_system_id == listed.pk, "the premise: this DMI does match the listing"

    scoped = _run(
        gpu_engineer, scope=["gpu"], run_id="cccccccc-0000-0000-0000-00000000000f",
    )

    assert scoped.listing_system_id is None

    services.approve_run(_ready(scoped, gpu_engineer), by=reviewer)

    listed.refresh_from_db()
    assert listed.attestation_count == before


# --- the other half: a claim needs evidence that could have failed ---------------------
#
# ``verdict()`` answers "did anything fail", and it says True when nothing failed *because nothing
# ran*. When scoping was added, the only GPU validation in the suite was ``validate.gpu.driver``,
# informational on purpose because an unbound card is not a defect. So a GPU-scoped run would have
# come out PASS on the strength of having seen a driver, and minted a vendor-tier attestation on
# the card. The four NVIDIA CUDA tests exist to be the evidence; this is the rule that requires it.


def test_a_claim_with_only_informational_results_certifies_nothing(gpu_engineer, reviewer):
    run = ingest.ingest_bundle(
        submitter=gpu_engineer, source="api",
        bundle_file=_bundle(
            ["gpu"],
            results=[
                f.validate_result("validate.cpu.functional"),
                f.validate_result(
                    "validate.gpu.driver", category="gpu", severity="informational",
                ),
            ],
        ),
    )
    assert run.verdict() is True, "the premise: nothing failed, because nothing gated"

    assert services.unevidenced_claims(run) == ["gpu"]
    assert services.certifies(run) is False

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    assert _attested() == {}, "a driver sighting is not evidence a card works"


def test_a_claim_with_a_gating_pass_certifies(gpu_engineer, reviewer):
    """The same run with one required GPU result that passed. This is the difference the CUDA
    tests make."""
    run = _run(gpu_engineer, scope=["gpu"])

    assert services.unevidenced_claims(run) == []

    services.approve_run(_ready(run, gpu_engineer), by=reviewer)

    assert _attested(), "a required GPU result that passed is evidence"


def test_an_informational_only_run_adds_nothing_to_an_already_listed_component(
    gpu_engineer, nvidia, reviewer
):
    """The exploitable half of the same rule, which the test above misses.

    ``test_a_claim_with_only_informational_results_certifies_nothing`` uses a fresh card, so its
    attestation is blocked where no release row exists yet, not by the evidence check. And a
    repeat by the *same* person dedups on (release, person), so it cannot show the bug either.
    The exploit needs a card that is already listed and a *second* verified-vendor member whose
    unevidenced run mints a brand new attestation, inflating the count and bumping the tier, on
    the strength of a driver sighting. The guard added to ``_apply_attestation`` stops it.
    """
    # A real evidenced run, so the GPU component is listed and carries a release row.
    services.approve_run(_ready(_run(gpu_engineer, scope=["gpu"]), gpu_engineer), by=reviewer)
    attestations_before = CommunityAttestation.objects.count()
    assert attestations_before >= 1, "premise: the evidenced run certified the card"

    # A different member of the same verified vendor, so any attestation they mint is a new row
    # rather than a dedup against the first submitter.
    other = User.objects.create_user("scope-nvidia-2", password="pw")
    VendorMembership.objects.create(
        user=other, vendor=nvidia, role=VendorMembership.ROLE_SUBMITTER,
    )
    informational = _run(
        other, scope=["gpu"],
        results=[
            f.validate_result("validate.cpu.functional"),
            f.validate_result("validate.gpu.driver", category="gpu", severity="informational"),
        ],
    )
    assert informational.verdict() is True
    assert services.certifies(informational) is False

    services.approve_run(_ready(informational, other), by=reviewer)

    assert CommunityAttestation.objects.count() == attestations_before, (
        "an unevidenced run must not mint an attestation, even for a listed card"
    )


def test_evidence_for_one_claim_is_not_evidence_for_another(gpu_engineer, reviewer):
    """A CPU test says nothing about a GPU. Claiming both needs both."""
    run = _run(gpu_engineer, scope=["cpu", "gpu"], results=[
        f.validate_result("validate.cpu.functional"),
    ])

    assert services.unevidenced_claims(run) == ["gpu"]
    assert services.certifies(run) is False


def test_a_kind_nothing_can_evidence_cannot_be_certified(gpu_engineer):
    """A claim is only as good as the tests behind it. A kind with no category mapped to it is
    unevidenceable, which is the safe direction to be wrong in: adding one means writing tests
    first, not adding a line to a dict."""
    run = _run(gpu_engineer, scope=["motherboard"])

    assert services.unevidenced_claims(run) == ["motherboard"]


def test_a_whole_machine_run_needs_no_scope_evidence(gpu_engineer, reviewer):
    """The rule is about claims, and a whole-machine run makes none of this kind. Every existing
    run in the database is this case."""
    run = _run(gpu_engineer)

    assert services.unevidenced_claims(run) == []
    assert services.certifies(run) is True


def test_a_failed_result_is_not_evidence_either(gpu_engineer):
    """It has to have passed. A GPU test that ran and failed already sinks the verdict, but the
    two conditions are separate and both are checked."""
    run = _run(gpu_engineer, scope=["gpu"], results=[
        f.validate_result("validate.cpu.functional"),
        f.validate_result(GPU_EVIDENCE[0], category="gpu", status="fail"),
    ])

    assert services.unevidenced_claims(run) == ["gpu"]


# --- what the GUI calls it ------------------------------------------------------------
#
# Reported by a submitter looking at a GPU-scoped run of a Dell OptiPlex: "I'm still being prompted
# in the GUI in several different ways as if it is a whole system run." Every one of those ways came
# from naming the run after its host. A run whose subject is a card, presented as a validation of the
# machine the card was sitting in, invites the submitter and the reviewer to answer for the machine.


def test_a_scoped_run_is_named_by_what_it_claims(gpu_engineer):
    """``display_name`` feeds the page heading, the dashboard card, the review queue, the feeds, and
    every run table, so this one property is most of the complaint."""
    run = _run(gpu_engineer, scope=["gpu"])

    # ``smi_name`` wins over the lspci string, which is ``gpu_identity``'s decision.
    assert run.gpu_model == "NVIDIA L40S"
    assert run.claim_subject == "NVIDIA L40S"
    assert run.display_name == "NVIDIA L40S"


def test_the_host_is_still_named_as_context(gpu_engineer):
    """Not hidden. Where a card was measured is exactly what a reviewer needs; it is being named as
    the *subject* that was wrong."""
    run = _run(gpu_engineer, scope=["gpu"])

    assert run.host_name == "Dell Inc. PowerEdge R760"
    assert run.host_name != run.display_name


def test_a_whole_machine_run_is_still_named_by_its_machine(gpu_engineer):
    """The unscoped path is untouched, which is most of the catalog."""
    run = _run(gpu_engineer)

    assert run.claim_subject == ""
    assert run.display_name == "Dell Inc. PowerEdge R760"
    assert run.display_name == run.host_name


def test_a_scoped_run_with_nothing_detected_still_has_a_name(gpu_engineer):
    """A run has to be findable in a list even when its subject was never identified. Asking for the
    name is ``missing_submission_details``'s job, not this property's."""
    report = f.make_report(run_types=["validate"], results=[
        f.validate_result(GPU_EVIDENCE[0], category=GPU_EVIDENCE[1]),
    ])
    report["run"]["claim_scope"] = ["gpu"]
    report["inventory"]["summary"]["gpus"] = []
    run = ingest.ingest_bundle(
        submitter=gpu_engineer, source="api", bundle_file=f.as_upload(f.build_bundle(report)),
    )

    assert run.claim_subject == ""
    assert run.display_name == run.host_name


def test_a_scoped_draft_and_a_whole_machine_draft_are_not_the_same_submission(gpu_engineer):
    """The same chassis is not the same claim, and grouping them did damage in both directions:
    ``merge_listing_proposal`` copied the machine's listing details onto the component claim, and
    ``approve_group`` swept it through review under "Submit all N runs of this machine"."""
    scoped = _run(gpu_engineer, scope=["gpu"])
    whole = _run(gpu_engineer)

    assert scoped.system_product == whole.system_product      # one machine
    assert list(services.sibling_draft_runs(scoped)) == []
    assert list(services.sibling_draft_runs(whole)) == []


def test_two_runs_of_the_same_scope_are_still_one_submission(gpu_engineer):
    """The batching feature itself is untouched: a submitter validating one card on two releases
    still answers once."""
    first = _run(gpu_engineer, scope=["gpu"], version_id="9.6")
    second = _run(gpu_engineer, scope=["gpu"], version_id="9.4")

    assert list(services.sibling_draft_runs(first)) == [second]
    assert list(services.sibling_draft_runs(second)) == [first]


# --- what the GUI asks for ------------------------------------------------------------
#
# The other half of the same report. Naming the run after its host was one cause; the other was
# every control that asked somebody to answer for the machine. A refusal deep in the service layer
# is the right backstop and a poor interface: ``assign_listing`` raised only once a reviewer had
# already picked a system out of a dropdown of every machine in the catalog.


def _client(user):
    from django.test import Client

    client = Client()
    client.force_login(user)
    return client


def test_the_submitter_is_not_asked_to_describe_the_machine(gpu_engineer):
    """The button that was reported. Every field on that form describes the machine, and a saved
    answer would be worse than none: ``listing_proposal`` feeds ``effective_vendor`` and
    ``effective_product``, so filling it in renames the run after the host it is not about."""
    from django.urls import reverse

    run = _run(gpu_engineer, scope=["gpu"])

    page = _client(gpu_engineer).get(run.get_absolute_url())
    body = page.content.decode()

    assert "Add listing details" not in body
    # And the form itself refuses, so a bookmarked URL cannot reach it either.
    form = _client(gpu_engineer).get(
        reverse("results:propose_listing", args=[run.uuid]), follow=True,
    )
    assert form.redirect_chain
    assert 'name="vendor_name"' not in form.content.decode()


def test_the_submitter_is_not_asked_for_the_hosts_catalog_details(gpu_engineer):
    """This one blocked submission outright: ``outstanding`` disables the submit button, and the
    case scoping exists for is a card in a guest whose SKU is not in the catalog and must never be
    added to it."""
    report = f.make_report(run_types=["validate"], results=[
        f.validate_result(GPU_EVIDENCE[0], category=GPU_EVIDENCE[1]),
    ])
    report["run"]["claim_scope"] = ["gpu"]
    report["inventory"]["summary"]["system"] = {
        "vendor": "Amazon EC2", "product": "g5.xlarge", "serial": "ec2xyz",
    }
    run = ingest.ingest_bundle(
        submitter=gpu_engineer, source="api", bundle_file=f.as_upload(f.build_bundle(report)),
    )

    assert services.missing_submission_details(run) == []


def test_a_scoped_run_with_no_subject_is_asked_for_one(gpu_engineer):
    """The one thing it does owe. Without a detected part there is nothing for
    ``create_listings_from_run`` to tie, so letting it through to review wastes a reviewer's pass."""
    report = f.make_report(run_types=["validate"], results=[
        f.validate_result(GPU_EVIDENCE[0], category=GPU_EVIDENCE[1]),
    ])
    report["run"]["claim_scope"] = ["gpu"]
    report["inventory"]["summary"]["gpus"] = []
    run = ingest.ingest_bundle(
        submitter=gpu_engineer, source="api", bundle_file=f.as_upload(f.build_bundle(report)),
    )

    outstanding = services.missing_submission_details(run)

    assert len(outstanding) == 1
    assert "nothing to certify" in outstanding[0]


def test_the_reviewer_is_not_offered_a_system_to_attach(gpu_engineer, reviewer):
    """Removed rather than offered and rejected. Deleting the field also unbinds it, so a
    hand-crafted POST cannot smuggle a system past the page and reach the service-layer refusal."""
    from lumina.results.forms import RunListingAssignForm

    run = _run(gpu_engineer, scope=["gpu"])
    system = System.objects.create(
        vendor=Vendor.objects.get_or_create(name="Dell Inc.")[0], name="PowerEdge R760",
    )

    assert "system" not in RunListingAssignForm(run=run).fields
    assert "machine_kind" not in RunListingAssignForm(run=run).fields
    # The whole-machine form is untouched.
    assert "system" in RunListingAssignForm(run=_run(gpu_engineer)).fields

    posted = RunListingAssignForm({"system": str(system.pk)}, run=run)
    assert posted.is_valid(), posted.errors
    assert "system" not in posted.cleaned_data


def test_the_reviewer_is_told_what_approving_will_do(gpu_engineer, reviewer):
    """``proposal_effect`` is machine-shaped throughout, and every consumer on the review page read
    it. It told a reviewer "This run is evidence about Dell PowerEdge R760"."""
    run = _run(gpu_engineer, scope=["gpu"])

    effect = services.proposal_effect(run)

    assert effect["scoped"] is True
    assert effect["creates_system"] is False
    assert effect["listing"] is None
    assert effect["host_name"] == "Dell Inc. PowerEdge R760"
    assert effect["parts"] == ["L40S"]
    # Nothing outstanding, because this run has a gating GPU pass.
    assert effect["unevidenced"] == []


def test_the_reviewer_is_warned_when_a_claim_certifies_nothing(gpu_engineer):
    """``verdict()`` cannot say this: it answers "did anything fail" and is True when nothing ran.
    A reviewer seeing PASS needs to know that approving moves no listing's standing."""
    run = _run(gpu_engineer, scope=["gpu"], results=[
        f.validate_result("validate.gpu.driver", category="gpu", severity="informational"),
    ])

    assert run.verdict() is True
    assert services.proposal_effect(run)["unevidenced"] == ["gpu"]
