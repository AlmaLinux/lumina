"""Show the lspci identity sources for a run's NICs and GPUs, and what the catalog names them.

A wrong catalog name for a NIC or GPU almost always traces to which of the lspci strings the naming
rule picked - vendor, device (the chip/controller), subsystem vendor, subsystem device (the card).
This prints all of them, from the exact source the tie logic reads (``categorized_devices`` ->
``nic_identity``/``gpu_identity``), so the choice can be seen rather than guessed at. Read-only.

    manage.py inspect_devices <run-uuid-or-pk>
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from lumina.results.device_inventory import categorized_devices
from lumina.results.models import TestRun
from lumina.results.pci_names import gpu_identity, nic_identity, pci_name

_ID_KEYS = ("vendor", "device", "subsystem_vendor", "subsystem_device")


class Command(BaseCommand):
    help = "Show the lspci identity sources for a run's NICs/GPUs and what each resolves to."

    def add_arguments(self, parser):
        parser.add_argument("run", help="TestRun UUID or numeric pk.")

    def handle(self, *args, **options):
        run = self._find(options["run"])
        summary = run.inventory.get("summary") or {}
        source = ("pci_devices (raw lspci enumeration)" if summary.get("pci_devices")
                  else "collector nics/gpus (bundle predates pci_devices)")
        self.stdout.write(
            f"Run {run.uuid}\n"
            f"  board   = {run.board_vendor or '-'} / {run.board_model or '-'}\n"
            f"  system  = {run.system_vendor or '-'} / {run.system_product or '-'}\n"
            f"  id source = {source}\n"
            "  (NIC model is the chip's 'device' - the controller, the same name lshw reports as\n"
            "   the product; 'subsystem_device' is a fallback only, since onboard it is the board)"
        )
        devices = categorized_devices(run)
        self._section("NICs", devices["nics"], nic_kind=True)
        self._section("GPUs", devices["gpus"], nic_kind=False)

    def _find(self, ident: str) -> TestRun:
        run = TestRun.objects.filter(uuid=ident).first()
        if run is None and ident.isdigit():
            run = TestRun.objects.filter(pk=int(ident)).first()
        if run is None:
            raise CommandError(f"No run with uuid or pk {ident!r}.")
        return run

    def _section(self, label, devices, *, nic_kind):
        self.stdout.write(f"\n{label}:")
        if not devices:
            self.stdout.write("  (none)")
            return
        for dev in devices:
            ids = dev.get("pci_ids") or {}
            name_field = dev.get("name") or "?"
            self.stdout.write(
                f"  {name_field} (pci {dev.get('pci') or '?'}) driver={dev.get('driver') or '-'}")
            if not nic_kind and dev.get("smi_name"):
                self.stdout.write(
                    f"      {'smi_name':<18} {dev['smi_name']} -> {pci_name(dev['smi_name']) or '(none)'}")
            vendor, model = nic_identity(dev) if nic_kind else gpu_identity(dev)
            for key in _ID_KEYS:
                raw = ids.get(key) or ""
                name = pci_name(raw)
                note = "   <- used as the model" if nic_kind and name and name == model else ""
                self.stdout.write(
                    f"      {key:<18} {raw or '(none)'} -> {name or '(no usable name)'}{note}")
            self.stdout.write(f"      => catalogued as: {vendor or '?'} / {model or '?'}")
