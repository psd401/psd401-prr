# ABOUTME: Google claim validation, least-privilege group lookup, role sync,
# ABOUTME: safe just-in-time provisioning, and session revocation helpers.
import re
from dataclasses import dataclass
from pathlib import Path

import google.auth
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone
from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from rest_framework.exceptions import PermissionDenied

from apps.audit.models import audit
from apps.configuration import runtime

from .models import (
    DirectStaffRoleGrant,
    GoogleGroupRoleMapping,
    StaffIdentity,
    StaffRole,
)

ALL_STAFF_ROLES = frozenset(StaffRole.values)
WORKFLOW_STAFF_ROLES = frozenset(
    {
        StaffRole.RECORDS_OFFICER,
        StaffRole.LIAISON,
        StaffRole.REVIEWER,
        StaffRole.ADMIN,
    }
)
MANAGED_GROUP_PREFIX = "prr-role:"
GROUPS_READONLY_SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"


class GoogleDirectoryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidatedGoogleIdentity:
    subject: str
    email: str
    first_name: str = ""
    last_name: str = ""


def validate_google_claims(claims, expected_domain):
    subject = str(claims.get("sub", "")).strip()
    email = str(claims.get("email", "")).strip().lower()
    hosted_domain = str(claims.get("hd", "")).strip().lower()
    if not subject or not email or claims.get("email_verified") not in (True, "true"):
        raise PermissionDenied("Google did not provide a verified staff identity.")
    if not expected_domain or hosted_domain != expected_domain.lower():
        raise PermissionDenied("This Google Workspace domain is not authorized.")
    return ValidatedGoogleIdentity(
        subject=subject,
        email=email,
        first_name=str(claims.get("given_name", "")).strip(),
        last_name=str(claims.get("family_name", "")).strip(),
    )


def role_group_name(role):
    if role not in StaffRole.values:
        raise ValueError(f"Unknown staff role: {role}")
    return f"{MANAGED_GROUP_PREFIX}{role}"


def staff_roles(user):
    if not user or not user.is_authenticated or not user.is_active or not user.is_staff:
        return set()
    if user.is_superuser:
        return set(ALL_STAFF_ROLES)
    roles = {
        name.removeprefix(MANAGED_GROUP_PREFIX)
        for name in user.groups.filter(name__startswith=MANAGED_GROUP_PREFIX).values_list("name", flat=True)
        if name.removeprefix(MANAGED_GROUP_PREFIX) in StaffRole.values
    }
    implicit = set(getattr(settings, "PRR_LOCAL_IMPLICIT_STAFF_ROLES", ()))
    if str(runtime.get("PRR_AUTH_MODE")).lower() == "local" and not roles:
        return implicit
    return roles


def has_staff_role(user, *roles):
    return bool(staff_roles(user).intersection(roles))


@transaction.atomic
def apply_staff_roles(user, roles):
    roles = set(roles)
    unknown = roles.difference(StaffRole.values)
    if unknown:
        raise ValueError(f"Unknown staff roles: {sorted(unknown)}")
    managed = Group.objects.filter(name__startswith=MANAGED_GROUP_PREFIX)
    user.groups.remove(*managed)
    for role in sorted(roles):
        group, _ = Group.objects.get_or_create(name=role_group_name(role))
        user.groups.add(group)


def _unique_username(email):
    User = get_user_model()
    base = re.sub(r"[^a-z0-9._-]", "-", email.split("@", 1)[0].lower()).strip("-") or "staff"
    candidate = base[:140]
    suffix = 1
    while User.objects.filter(username=candidate).exists():
        suffix += 1
        tail = f"-{suffix}"
        candidate = f"{base[:150 - len(tail)]}{tail}"
    return candidate


@transaction.atomic
def provision_google_user(claims, roles):
    identity = validate_google_claims(
        claims, runtime.get("PRR_GOOGLE_HOSTED_DOMAIN")
    )
    roles = set(roles)
    if not roles:
        raise PermissionDenied("Your account is not assigned to a PRR staff role.")

    existing_identity = StaffIdentity.objects.select_related("user").filter(
        google_subject=identity.subject
    ).first()
    if existing_identity:
        user = existing_identity.user
        if not user.is_active:
            raise PermissionDenied(
                "This staff account has been disabled by an administrator."
            )
    else:
        matches = list(get_user_model().objects.filter(email__iexact=identity.email))
        if matches:
            if len(matches) != 1:
                raise PermissionDenied("An administrator must resolve the existing account before login.")
            user = matches[0]
            if not user.is_active:
                raise PermissionDenied(
                    "This staff account has been disabled by an administrator."
                )
            staff_identity = getattr(user, "staff_identity", None)
            if not staff_identity or not staff_identity.allow_next_google_link:
                raise PermissionDenied("An administrator must approve linking this existing account.")
            staff_identity.google_subject = identity.subject
            staff_identity.source = StaffIdentity.Source.GOOGLE
            staff_identity.allow_next_google_link = False
            staff_identity.save(update_fields=["google_subject", "source", "allow_next_google_link", "updated_at"])
        else:
            user = get_user_model().objects.create_user(
                username=_unique_username(identity.email),
                email=identity.email,
                first_name=identity.first_name,
                last_name=identity.last_name,
                is_staff=True,
            )
            user.set_unusable_password()
            user.save(update_fields=["password"])
            StaffIdentity.objects.create(
                user=user,
                source=StaffIdentity.Source.GOOGLE,
                google_subject=identity.subject,
            )

    user.email = identity.email
    user.first_name = identity.first_name
    user.last_name = identity.last_name
    user.is_staff = True
    user.save(update_fields=["email", "first_name", "last_name", "is_staff"])
    apply_staff_roles(user, roles)
    staff_identity = user.staff_identity
    staff_identity.last_group_check_at = timezone.now()
    staff_identity.last_group_error = ""
    staff_identity.save(update_fields=["last_group_check_at", "last_group_error", "updated_at"])
    audit("identity.google_login", target=str(user.pk), actor=user, roles=sorted(roles))
    return user


class GoogleGroupDirectory:
    """Read direct membership in only the administrator-configured groups."""

    def __init__(self, api=None):
        self.api = api

    def _api(self):
        if self.api is not None:
            return self.api
        credentials_file = runtime.get("PRR_GOOGLE_GROUP_CREDENTIALS_FILE")
        try:
            if credentials_file:
                credentials, _ = google.auth.load_credentials_from_file(
                    str(Path(credentials_file)), scopes=[GROUPS_READONLY_SCOPE]
                )
            else:
                credentials, _ = google.auth.default(scopes=[GROUPS_READONLY_SCOPE])
            self.api = build("cloudidentity", "v1", credentials=credentials, cache_discovery=False)
            return self.api
        except (OSError, ValueError, GoogleAuthError, HttpError) as exc:
            raise GoogleDirectoryUnavailable("Google group credentials are unavailable.") from exc

    def is_direct_member(self, group_email, member_email):
        try:
            group = self._api().groups().lookup(groupKey_id=group_email).execute()
            parent = group["name"]
            self._api().groups().memberships().lookup(
                parent=parent,
                memberKey_id=member_email,
            ).execute()
            return True
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                return False
            raise GoogleDirectoryUnavailable(
                "Google group membership could not be verified."
            ) from exc
        except (GoogleAuthError, OSError, KeyError) as exc:
            raise GoogleDirectoryUnavailable("Google group membership could not be verified.") from exc

    def roles_for_email(self, email):
        roles = set()
        for mapping in GoogleGroupRoleMapping.objects.filter(active=True):
            if self.is_direct_member(mapping.google_group, email):
                roles.add(mapping.role)
        return roles


def direct_roles_for_email(email):
    """Roles granted to this exact address without a Google group.

    Touches no Google API: these grants are application records about a named
    person, which is why they keep working when the directory does not.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return set()
    return set(
        DirectStaffRoleGrant.objects.filter(
            active=True,
            email__iexact=normalized,
        ).values_list("role", flat=True)
    )


def resolved_roles_for_email(email, directory=None):
    """Every role this address holds, from groups and direct grants together.

    Grants are additive, so this is a union and can only widen what the
    directory says.

    If Google is unreachable the caller normally fails closed, and it still
    does for anyone whose access depends on a group. Somebody holding a direct
    grant is different: their authority was never Google's to answer for, so a
    Workspace outage must not lock them out of the records system. They
    continue on their grants alone - which may be narrower than usual, never
    wider - and the directory error is recorded for the identity.
    """
    granted = direct_roles_for_email(email)
    try:
        return (directory or GoogleGroupDirectory()).roles_for_email(email) | granted
    except GoogleDirectoryUnavailable:
        if granted:
            return granted
        raise


def refresh_google_roles(user, directory=None):
    identity = user.staff_identity
    if identity.source != StaffIdentity.Source.GOOGLE:
        return staff_roles(user)
    old_roles = staff_roles(user)
    try:
        roles = resolved_roles_for_email(user.email, directory)
    except GoogleDirectoryUnavailable as exc:
        identity.last_group_error = str(exc)
        identity.save(update_fields=["last_group_error", "updated_at"])
        raise
    apply_staff_roles(user, roles)
    identity.last_group_check_at = timezone.now()
    identity.last_group_error = ""
    identity.save(update_fields=["last_group_check_at", "last_group_error", "updated_at"])
    if roles != old_roles:
        audit(
            "identity.roles_changed",
            target=str(user.pk),
            actor=user,
            before=sorted(old_roles),
            after=sorted(roles),
        )
    return roles


def revoke_user_sessions(user):
    for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
        if str(session.get_decoded().get("_auth_user_id")) == str(user.pk):
            session.delete()


def expire_google_authorizations(*, keep_sessions_of=None):
    """Force every Google principal to refresh after a mapping change.

    ``keep_sessions_of`` is the administrator making the change. Their
    authorization is still expired, so the revalidation middleware recomputes
    their roles on their very next request, but their session survives:
    deleting it signed them out between the writes of a single save and
    failed the rest with "Authentication credentials were not provided."
    """
    keep_pk = getattr(keep_sessions_of, "pk", None)
    identities = StaffIdentity.objects.filter(
        source=StaffIdentity.Source.GOOGLE
    ).select_related("user")
    for identity in identities:
        identity.last_group_check_at = None
        identity.save(update_fields=["last_group_check_at", "updated_at"])
        if identity.user_id != keep_pk:
            revoke_user_sessions(identity.user)
