"""Add device_pci_id to BenchmarkResult and backfill it from each run's inventory.

device_pci_id is the "vendor:device" of the card a GPU benchmark ran on, resolved by tying the
clpeak device name back to the run's inventory GPUs. It is the grouping key the leaderboard and
compare page key on, so a card's Vulkan and OpenCL figures group as one GPU rather than splitting
into two device names. The backfill recomputes it for rows already ingested, so the split heals
without re-running the suite; a row that cannot be pinned down keeps a blank id and falls back to
device_model exactly as before.
"""

from django.db import migrations, models


def backfill_device_pci_id(apps, schema_editor):
    # Imported at run time (a pure function over inventory dicts). A later change to the tie rule is
    # welcome to recompute more rows on a fresh migrate; this pass fills what it can from what
    # exists now.
    from lumina.results.pci_names import benchmark_gpu_pci_id

    BenchmarkResult = apps.get_model("results", "BenchmarkResult")
    TestRun = apps.get_model("results", "TestRun")

    run_ids = list(
        BenchmarkResult.objects.exclude(device_raw="")
        .values_list("run_id", flat=True)
        .distinct()
    )
    for run in TestRun.objects.filter(pk__in=run_ids).iterator():
        gpus = (run.inventory.get("summary") or {}).get("gpus") or []
        by_raw: dict[str, str] = {}
        to_update = []
        for row in BenchmarkResult.objects.filter(run=run).exclude(device_raw=""):
            if row.device_raw not in by_raw:
                by_raw[row.device_raw] = benchmark_gpu_pci_id(row.device_raw, gpus)
            pci_id = by_raw[row.device_raw]
            if pci_id and row.device_pci_id != pci_id:
                row.device_pci_id = pci_id
                to_update.append(row)
        if to_update:
            BenchmarkResult.objects.bulk_update(to_update, ["device_pci_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("results", "0007_drop_software_gpu_benchmarks"),
    ]

    operations = [
        migrations.AddField(
            model_name="benchmarkresult",
            name="device_pci_id",
            field=models.CharField(blank=True, db_index=True, max_length=32),
        ),
        # No reverse: reversing the AddField drops the column and its data with it.
        migrations.RunPython(backfill_device_pci_id, migrations.RunPython.noop),
    ]
