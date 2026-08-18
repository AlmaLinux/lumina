"""Remove GPU benchmark rows recorded against a CPU software rasterizer.

Before ``is_software_gpu`` gated ingest, a bundle whose clpeak run enumerated llvmpipe (lavapipe on
Vulkan, or rusticl-on-llvmpipe on OpenCL, both reported as "llvmpipe") stored it as a GPU result,
so "llvmpipe" appeared as a GPU in the catalog and on the compare page. Those rows are CPU compute
mislabeled as a graphics card; this deletes the ones already in the database, matched by the same
software-rasterizer names ingest now screens. The raw submitted bundles are untouched, so the
evidence remains and a re-ingest under the new gate simply omits them.
"""

from django.db import migrations, models

# Kept in step with ``lumina.results.component_match._SOFTWARE_GPU_MARKERS``. Inlined rather than
# imported so this migration deletes exactly what it targeted when written, even if that list later
# grows: a migration must not change shape after the fact.
_SOFTWARE_GPU_MARKERS = ("llvmpipe", "swrast", "softpipe", "lavapipe", "swiftshader")


def drop_software_gpu_results(apps, schema_editor):
    BenchmarkResult = apps.get_model("results", "BenchmarkResult")
    query = models.Q()
    for marker in _SOFTWARE_GPU_MARKERS:
        query |= models.Q(device_raw__icontains=marker)
        query |= models.Q(device_model__icontains=marker)
    BenchmarkResult.objects.filter(query).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("results", "0006_benchmarkresult_per_device"),
    ]

    # No reverse: a deleted mislabeled row is not worth recreating, and there is nothing to restore
    # it from that the ingest gate would not immediately drop again.
    operations = [
        migrations.RunPython(drop_software_gpu_results, migrations.RunPython.noop),
    ]
