# ABOUTME: The in-app settings store — one current value per key plus an
# ABOUTME: append-only history row carrying actor, reason, and both values.
"""One current value per key, and every change that ever produced one.

``AppSetting`` is the source of truth a read resolves against.
``AppSettingChange`` is the evidence: append-only like ``AuditLog`` and
``StatusEvent``, because "who turned the outbound kill switch off, when, and
why" is a question a district may have to answer under oath.
"""

from django.conf import settings
from django.db import models

from apps.append_only import AppendOnlyManager, AppendOnlyModelMixin


class AppSetting(models.Model):
    """The current value of one application-managed setting."""

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key}={self.value!r}"


class AppSettingChange(AppendOnlyModelMixin, models.Model):
    """One immutable record of a setting changing value."""

    class Source(models.TextChoices):
        ADMIN = "admin", "Application administrator"
        SYSTEM_IMPORT = "system_import", "System import of the deployed value"

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    key = models.CharField(max_length=100, db_index=True)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField()
    reason = models.TextField()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.ADMIN,
    )

    objects = AppendOnlyManager()

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["key", "-at"])]


class AppSecret(models.Model):
    """One write-only setting, encrypted, with who installed it and when.

    Deliberately not an ``AppSetting`` row. ``AppSetting.value`` is read by
    the Settings page, serialised into the API response, and copied verbatim
    into every history row; a secret must appear in none of those. Keeping
    the ciphertext in its own table means the disclosure paths are the ones
    this model is asked for by name, rather than every path that already
    handles a setting.

    ``fingerprint`` is what an administrator compares against. It is an HMAC
    under the encryption key, so holding the database does not let anyone
    confirm a guess at the value.
    """

    key = models.CharField(max_length=100, unique=True)
    ciphertext = models.TextField()
    fingerprint = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="app_secrets_set",
    )

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key} (set, {self.fingerprint})"


class SettingVerification(models.Model):
    """The result of the last live check of one credential or identity.

    A credential the district saves is kept even when it cannot authenticate
    — refusing the save would leave the old, equally broken value in place
    and tell an administrator nothing. What the save does instead is write
    down that the check failed, so the Settings page can say "saved,
    unverified" rather than implying the integration works. Nothing reads
    this to decide whether to act: every consumer already fails closed on a
    credential it cannot use.
    """

    key = models.CharField(max_length=100, unique=True)
    checked_at = models.DateTimeField(auto_now=True)
    ok = models.BooleanField(default=False)
    detail = models.TextField(blank=True)

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key}: {'verified' if self.ok else 'unverified'}"
