"""Shared provisioning and exact authorization for integration principals."""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.db import transaction
from rest_framework.authtoken.models import Token

MCP_PERMISSION = ("records", "read_staff_api")
OUTBOUND_PERMISSION = (
    "correspondence",
    "deliver_outbound_correspondence",
)
SYSTEM_OUTBOUND_TRANSPORT_USERNAME = "prr-gmail-outbound-transport"
SYSTEM_INBOUND_TRANSPORT_USERNAME = "prr-gmail-inbound-transport"
INBOUND_PERMISSION = (
    "correspondence",
    "submit_inbound_correspondence",
)


@dataclass(frozen=True)
class IntegrationIdentityDefinition:
    id: str
    label: str
    permission: tuple[str, str]
    require_existing_permission: bool = False

    @property
    def permission_name(self):
        return ".".join(self.permission)


INTEGRATION_IDENTITY_DEFINITIONS = (
    IntegrationIdentityDefinition(
        id="mcp_readonly",
        label="Read-only MCP",
        permission=MCP_PERMISSION,
    ),
)
INTEGRATION_IDENTITY_BY_ID = {
    definition.id: definition
    for definition in INTEGRATION_IDENTITY_DEFINITIONS
}


class IntegrationIdentityProvisionError(ValueError):
    """A named account cannot safely receive the requested exact scope."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProvisionedIntegrationIdentity:
    definition: IntegrationIdentityDefinition
    user: object
    token: Token
    created_user: bool
    rotated: bool


@dataclass(frozen=True)
class IntegrationIdentityAccountStatus:
    username: str
    token_created_at: object | None
    scope_valid: bool


def _current_identity(user, *, lock):
    user_id = getattr(user, "pk", None)
    if user_id is None:
        return None
    database = getattr(user._state, "db", None) or "default"
    connection = transaction.get_connection(database)
    queryset = get_user_model().objects.using(database)
    if lock and connection.in_atomic_block:
        queryset = queryset.select_for_update(of=("self",))
    current = queryset.filter(pk=user_id).first()
    if current is None:
        return None
    if lock and connection.in_atomic_block:
        for relation in (current.groups, current.user_permissions):
            list(
                relation.through.objects.using(database)
                .select_for_update()
                .filter(user_id=current.pk)
                .order_by("pk")
                .values_list("pk", flat=True)
            )
    return current


def is_exact_service_identity_row(user, permission):
    """Check one current user row for one and only one direct permission."""
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
        or not user.is_active
        or user.is_staff
        or user.is_superuser
        or user.has_usable_password()
        or user.groups.exists()
    ):
        return False
    direct_permissions = set(
        user.user_permissions.values_list(
            "content_type__app_label",
            "codename",
        )
    )
    return direct_permissions == {permission}


def get_integration_identity_definition(identity_id):
    try:
        return INTEGRATION_IDENTITY_BY_ID[identity_id]
    except KeyError as exc:
        raise IntegrationIdentityProvisionError(
            "unknown_identity",
            "Unknown integration identity.",
        ) from exc


def _permission_for(definition):
    app_label, codename = definition.permission
    return Permission.objects.get(
        content_type__app_label=app_label,
        codename=codename,
    )


def integration_identity_statuses():
    """Return four scope categories without ever loading token key values."""
    user_model = get_user_model()
    statuses = []
    for definition in INTEGRATION_IDENTITY_DEFINITIONS:
        permission = _permission_for(definition)
        users = list(
            user_model._default_manager.filter(
                user_permissions=permission,
            ).order_by("username", "pk")
        )
        token_created_by_user = dict(
            Token.objects.filter(user_id__in=[user.pk for user in users])
            .values_list("user_id", "created")
        )
        accounts = [
            IntegrationIdentityAccountStatus(
                username=user.get_username(),
                token_created_at=token_created_by_user.get(user.pk),
                scope_valid=is_exact_service_identity_row(
                    user,
                    definition.permission,
                ),
            )
            for user in users
        ]
        statuses.append((definition, accounts))
    return statuses


@transaction.atomic
def provision_integration_identity(
    identity_id,
    username,
    *,
    rotate=False,
    require_existing_token=False,
):
    """Create or rotate one exact-scope DRF token through the canonical path."""
    definition = get_integration_identity_definition(identity_id)
    username = str(username).strip()
    if not username:
        raise IntegrationIdentityProvisionError(
            "blank_username",
            "username cannot be blank",
        )
    if username in {
        SYSTEM_OUTBOUND_TRANSPORT_USERNAME,
        SYSTEM_INBOUND_TRANSPORT_USERNAME,
    }:
        raise IntegrationIdentityProvisionError(
            "reserved_system_identity",
            "An in-process Gmail transport identity cannot receive a token.",
        )

    target_permission = _permission_for(definition)
    user_model = get_user_model()
    user, created = user_model._default_manager.get_or_create(
        username=username,
    )
    if not created:
        user = user_model._default_manager.select_for_update().get(pk=user.pk)

    if created:
        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        user.set_unusable_password()
        user.save()
    else:
        has_target_permission = user.user_permissions.filter(
            pk=target_permission.pk,
        ).exists()
        extra_permissions = user.user_permissions.exclude(
            pk=target_permission.pk,
        ).exists()
        if (
            user.is_staff
            or user.is_superuser
            or user.has_usable_password()
            or user.groups.exists()
            or extra_permissions
            or (
                definition.require_existing_permission
                and not has_target_permission
            )
        ):
            raise IntegrationIdentityProvisionError(
                "unsafe_existing_account",
                (
                    "Refusing to repurpose an account with interactive or "
                    "additional access, or without the required identity "
                    "marker. Choose a new integration-specific username."
                ),
            )
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])

    user.user_permissions.add(target_permission)
    existing = Token.objects.select_for_update().filter(user=user).first()
    if existing and not rotate:
        raise IntegrationIdentityProvisionError(
            "token_exists",
            (
                "This integration identity already has a token. Use rotate "
                "to revoke it and issue a replacement."
            ),
        )
    if rotate and require_existing_token and existing is None:
        raise IntegrationIdentityProvisionError(
            "token_missing",
            "This integration identity has no token to rotate.",
        )
    if existing:
        existing.delete()
    token = Token.objects.create(user=user)
    return ProvisionedIntegrationIdentity(
        definition=definition,
        user=user,
        token=token,
        created_user=created,
        rotated=bool(existing),
    )


def _current_presented_token(token, *, user, lock):
    token_key = getattr(token, "pk", None)
    if token_key is None or user is None:
        return None
    database = getattr(user._state, "db", None) or "default"
    queryset = Token.objects.using(database)
    if lock and transaction.get_connection(database).in_atomic_block:
        queryset = queryset.select_for_update(of=("self",))
    return queryset.filter(
        pk=token_key,
        user_id=user.pk,
    ).first()


def is_exact_mcp_identity(user):
    """Return whether the current row is the isolated read-only MCP principal."""
    return is_exact_service_identity_row(
        _current_identity(user, lock=False),
        MCP_PERMISSION,
    )


def is_exact_outbound_identity(user):
    """Return whether ``user`` is the isolated outbound transport principal."""
    return is_exact_service_identity_row(
        _current_identity(user, lock=False),
        OUTBOUND_PERMISSION,
    )


def is_exact_inbound_identity(user):
    """Return whether ``user`` is the isolated inbound transport principal."""
    return is_exact_service_identity_row(
        _current_identity(user, lock=False),
        INBOUND_PERMISSION,
    )


def _require_exact_transport_identity(
    user,
    *,
    credential,
    permission,
    system_username,
    label,
):
    current = _current_identity(user, lock=True)
    system_transport = (
        current is not None
        and current.get_username() == system_username
    )
    credential_is_current = (
        credential is not None
        and _current_presented_token(
            credential,
            user=current,
            lock=True,
        )
        is not None
    )
    if (
        not is_exact_service_identity_row(current, permission)
        or not (
            (credential is None and system_transport)
            or credential_is_current
        )
    ):
        raise PermissionDenied(
            f"An exact {label} transport identity is required."
        )
    return current


def require_exact_outbound_identity(user, *, credential=None):
    return _require_exact_transport_identity(
        user,
        credential=credential,
        permission=OUTBOUND_PERMISSION,
        system_username=SYSTEM_OUTBOUND_TRANSPORT_USERNAME,
        label="outbound-correspondence",
    )


def require_exact_inbound_identity(user, *, credential=None):
    return _require_exact_transport_identity(
        user,
        credential=credential,
        permission=INBOUND_PERMISSION,
        system_username=SYSTEM_INBOUND_TRANSPORT_USERNAME,
        label="inbound-correspondence",
    )
