"""The AlmaLinux majors this catalog certifies against.

Nothing created these outside ``seed_devstack``, so a production install came up with an empty
table - and with it no release filter on browse, no releases to tick on a submission, and no row
for a run's evidence to hang off. Every certification here is a statement about a major, so with
no majors there was nothing any of it could state.

``supported`` and ``latest_minor`` are facts that move: a major reaches end of life, a minor
ships. This only ever creates, so a deployment that has retired 8 or raised 9 keeps its own
answer, and ``latest_minor`` is a starting point an admin raises rather than a claim frozen here.
"""
from django.db import migrations

# (major, supported, latest_minor as of this migration).
#
# ``latest_minor`` is the newest minor that has actually shipped, and the only thing it does is
# lift the "enablement lands in 10.3" disclaimer off hardware proved on AlmaLinux Kitten. Stale by
# a minor is harmless; it just leaves that note up a little longer than it needs to be.
RELEASES = [
    (8, True, 10),
    (9, True, 8),
    (10, True, 2),
]


def seed(apps, schema_editor):
    model = apps.get_model("releases", "AlmaLinuxRelease")
    for major, supported, latest_minor in RELEASES:
        model.objects.get_or_create(
            major=major, defaults={"supported": supported, "latest_minor": latest_minor},
        )


def unseed(apps, schema_editor):
    apps.get_model("releases", "AlmaLinuxRelease").objects.filter(
        major__in=[major for major, _supported, _minor in RELEASES],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [("releases", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
