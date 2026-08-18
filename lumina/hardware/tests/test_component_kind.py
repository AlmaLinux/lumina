"""Component taxonomy by kind (cpu/nic/storage/other).

- ``Component.kind`` is a StrEnum-backed field that defaults to ``other``.
- Systems can attach CPU Components via ``System.cpus`` (M2M limited to
  components whose kind is ``cpu``). Attaching a non-CPU component raises.
- ``Component.cpus`` / ``Component.nics`` / ... are convenience querysets
  returned by a single classmethod ``Component.of_kind(kind)``.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from lumina.hardware.models import Component, ComponentKind, System
from lumina.vendors.models import Vendor

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def intel():
    return Vendor.objects.get_or_create(name="Intel")[0]


@pytest.fixture
def dell():
    return Vendor.objects.create(name="Dell")


@pytest.fixture
def cpu(intel):
    return Component.objects.create(
        name="Xeon Platinum 8480+",
        vendor=intel,
        model_number="8480+",
        kind=ComponentKind.cpu,
    )


@pytest.fixture
def nic(dell):
    return Component.objects.create(
        name="BCM57414",
        vendor=dell,
        model_number="BCM57414",
        kind=ComponentKind.nic,
    )


@pytest.fixture
def system(dell):
    return System.objects.create(name="PowerEdge R750", vendor=dell, model_number="R750")


class ComponentKindTests:
    def test_default_kind_is_other(self, dell):
        c = Component.objects.create(name="X", vendor=dell, model_number="X")
        assert c.kind == ComponentKind.other

    def test_of_kind_filters(self, cpu, nic):
        assert cpu in Component.of_kind(ComponentKind.cpu)
        assert nic not in Component.of_kind(ComponentKind.cpu)
        assert nic in Component.of_kind(ComponentKind.nic)


class SystemCpusTests:
    def test_attach_cpu_component(self, system, cpu):
        system.cpus.add(cpu)
        assert list(system.cpus.all()) == [cpu]

    def test_reverse_accessor(self, system, cpu):
        system.cpus.add(cpu)
        assert system in cpu.cpu_of_systems.all()

    def test_attaching_non_cpu_raises(self, system, nic):
        # Enforce at the service layer rather than DB schema, because a
        # pure M2M with a filtered through-model would block non-CPUs from
        # being referenced anywhere - too strict. attach_cpu() is the
        # single choke-point that the submit form must use.
        from lumina.hardware.services import attach_cpu

        with pytest.raises(ValueError):
            attach_cpu(system, nic)

    def test_attach_cpu_helper_attaches_and_returns(self, system, cpu):
        from lumina.hardware.services import attach_cpu

        attach_cpu(system, cpu)
        assert cpu in system.cpus.all()


def test_kind_labels_keep_acronyms_uppercase():
    """CPU/GPU/NIC are acronyms; capitalize() would render them "Cpu"/"Nic"."""
    from lumina.hardware.models import ComponentKind

    labels = dict(ComponentKind.choices())
    assert labels["cpu"] == "CPU"
    assert labels["gpu"] == "GPU"
    assert labels["nic"] == "NIC"
    # ordinary words are simply capitalized
    assert labels["motherboard"] == "Motherboard"
    assert labels["storage"] == "Storage"
    # every kind has a label and none of them are lowercase
    assert all(label and label[0].isupper() for label in labels.values())


@pytest.mark.django_db
def test_get_kind_display_uses_the_canonical_labels():
    from lumina.hardware.models import Component, ComponentKind
    from lumina.vendors.models import Vendor

    vendor = Vendor.objects.get_or_create(name="Intel")[0]
    cpu = Component.objects.create(
        name="Xeon Gold 6430", vendor=vendor, kind=ComponentKind.cpu.value
    )
    nic = Component.objects.create(
        name="X710", vendor=vendor, kind=ComponentKind.nic.value
    )
    assert cpu.get_kind_display() == "CPU"
    assert nic.get_kind_display() == "NIC"
