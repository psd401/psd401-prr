"""Append-only audit log. No update/delete paths exist in application code;
add a DB trigger (or pgaudit) in production to enforce immutability at the
database layer too — assume every row may be produced in PRA litigation."""
from django.conf import settings
from django.db import models

from apps.append_only import AppendOnlyManager, AppendOnlyModelMixin


class AuditLog(AppendOnlyModelMixin, models.Model):
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100, db_index=True)   # e.g. "request.created", "vault.export.created"
    target = models.CharField(max_length=200, blank=True)      # e.g. "PRR-2026-0042"
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["-at"]


def audit(action: str, target: str = "", actor=None, ip_address=None, **detail):
    """Convenience helper: audit("request.created", target=prr.number, actor=user)"""
    return AuditLog.objects.create(
        action=action,
        target=target,
        actor=actor,
        detail=detail,
        ip_address=ip_address,
    )
