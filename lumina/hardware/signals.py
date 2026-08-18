"""Deleting a listing on a database that cannot defer constraint checks.

Reported as a 500 from the admin, and reproducible on MariaDB and not on SQLite, which is why the
whole suite passed while deleting any component or system in production failed.

Django's ``CASCADE`` handler does this, in ``db/models/deletion.py``::

    if field.null and not connections[using].features.can_defer_constraint_checks:
        collector.add_field_update(field, None, sub_objs)

A nullable cascading foreign key is set to NULL *before* its row is deleted, to break the
reference in a database that checks constraints statement by statement. Every "exactly one
listing" check constraint in this project spans exactly such a pair of nullable columns - a row
belongs either to a System or to a Component and never to both or to neither - so that
intermediate UPDATE produces a row with neither, and the constraint refuses it.

The listing is deleted here before the collector reaches its update pass, which runs after
pre_delete and before the deletes. The rows would have gone anyway - they cascade - so the end
state is identical; they simply go one statement earlier, and there is no moment at which one of
them belongs to nothing.

Derived from the relations rather than listed, so a table added later with the same shape is
covered without anybody remembering this file exists. Harmless for a nullable cascading relation
with no such constraint: the same rows, the same end state, one query sooner.
"""
from __future__ import annotations

from django.db import models
from django.db.models.signals import pre_delete
from django.dispatch import receiver


def _cascading_nullable_relations(model):
    """Reverse relations Django would NULL before deleting, on MySQL and MariaDB."""
    for related in model._meta.related_objects:
        field = related.field
        if field.many_to_one and field.null and field.remote_field.on_delete is models.CASCADE:
            yield field


@receiver(pre_delete, sender="hardware.System", dispatch_uid="listing_delete_system")
@receiver(pre_delete, sender="hardware.Component", dispatch_uid="listing_delete_component")
def delete_dependent_rows_first(sender, instance, **kwargs) -> None:
    for field in _cascading_nullable_relations(sender):
        # ``_base_manager``: a default manager that filters would leave rows behind for the
        # collector to null, which is the whole problem.
        field.model._base_manager.filter(**{field.name: instance}).delete()
