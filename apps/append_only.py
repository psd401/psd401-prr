"""Reusable application-layer guards for evidentiary append-only models."""

from django.core.exceptions import ValidationError
from django.db import models

APPEND_ONLY_MESSAGE = (
    "This evidentiary record is append-only and cannot be changed or deleted."
)


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError(APPEND_ONLY_MESSAGE)

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError(APPEND_ONLY_MESSAGE)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise ValidationError(APPEND_ONLY_MESSAGE)
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def delete(self):
        raise ValidationError(APPEND_ONLY_MESSAGE)


class AppendOnlyManager(models.Manager.from_queryset(AppendOnlyQuerySet)):
    pass


class AppendOnlyModelMixin:
    """Allow inserts while rejecting model-instance rewrites and deletion."""

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(APPEND_ONLY_MESSAGE)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(APPEND_ONLY_MESSAGE)
