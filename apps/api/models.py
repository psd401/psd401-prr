# ABOUTME: Staff identity records and administrator-controlled mappings from
# ABOUTME: district Google groups to stable application roles.
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.configuration import runtime


class StaffRole(models.TextChoices):
    RECORDS_OFFICER = "records_officer", "Records Officer"
    INTAKE_CLERK = "intake_clerk", "Intake Clerk"
    LIAISON = "liaison", "Department Liaison"
    REVIEWER = "reviewer", "Reviewer"
    ADMIN = "admin", "Application Admin"


class StaffIdentity(models.Model):
    class Source(models.TextChoices):
        LOCAL = "local", "Local development / break glass"
        GOOGLE = "google", "Google Workspace OIDC"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_identity",
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.LOCAL)
    google_subject = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        help_text="Immutable Google sub claim. Never use email as the identity key.",
    )
    allow_next_google_link = models.BooleanField(
        default=False,
        help_text="One-use approval to attach this existing account at its next verified Google login.",
    )
    last_group_check_at = models.DateTimeField(null=True, blank=True)
    last_group_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} ({self.source})"


class GoogleGroupRoleMapping(models.Model):
    role = models.CharField(max_length=30, choices=StaffRole.choices)
    google_group = models.EmailField(
        help_text="Existing Google Workspace group whose direct members receive this role."
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "google_group"]
        constraints = [
            models.UniqueConstraint(
                fields=["role", "google_group"],
                name="api_unique_google_group_role_mapping",
            ),
        ]

    def clean(self):
        super().clean()
        self.google_group = self.google_group.strip().lower()
        domain = self.google_group.rpartition("@")[2]
        if domain not in runtime.get("PRR_GOOGLE_GROUP_ALLOWED_DOMAINS"):
            raise ValidationError(
                {"google_group": "Role groups must use an approved Workspace domain."}
            )

    def __str__(self):
        return f"{self.get_role_display()} <- {self.google_group}"


class DirectStaffRoleGrant(models.Model):
    """A named individual's role, granted without a Google group.

    Group membership is the normal path and stays the default. This exists for
    the person who legitimately needs the system but belongs to no group that
    can be synced, where inventing a group - or dropping them into an unrelated
    one - would be worse governance than naming them explicitly.

    It is additive: a grant is unioned with whatever the configured groups
    already say, never subtracted from them. Deactivating a grant removes the
    role at the holder's next authorization refresh, the same as losing group
    membership does.
    """

    email = models.EmailField(
        help_text="The exact Workspace address this grant applies to.",
    )
    role = models.CharField(max_length=30, choices=StaffRole.choices)
    active = models.BooleanField(default=True)
    reason = models.CharField(
        max_length=500,
        help_text=(
            "Why this person is granted a role directly instead of through a "
            "group. Read during access reviews."
        ),
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="direct_staff_role_grants",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["email", "role"]
        constraints = [
            models.UniqueConstraint(
                fields=["email", "role"],
                name="api_unique_direct_staff_role_grant",
            ),
        ]

    def __str__(self):
        return f"{self.email} -> {self.role}"


class PersonalAccessToken(models.Model):
    """Named, expiring staff credential whose secret is never stored."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="personal_access_tokens",
    )
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=12, unique=True)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower("name"),
                "user",
                condition=models.Q(revoked_at__isnull=True),
                name="api_unique_live_pat_name_per_user",
            ),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.name} ({self.prefix})"
