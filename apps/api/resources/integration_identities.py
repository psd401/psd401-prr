# ABOUTME: Session-only Application-Admin lifecycle for integration credentials.
# ABOUTME: Secrets are returned once; metadata, scope checks, and audit omit keys.
from django.db import transaction
from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.api.handoffs import human_only_handoff
from apps.api.identity import StaffRole, has_staff_role
from apps.audit.models import audit
from apps.records import service_identities

IDENTITY_ID_PARAMETER = OpenApiParameter(
    name="id",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.PATH,
    enum=[
        definition.id
        for definition
        in service_identities.INTEGRATION_IDENTITY_DEFINITIONS
    ],
)


class IntegrationIdentityConsolePermission(BasePermission):
    """Let token callers reach the handoff; sessions use the view role policy."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if isinstance(request.successful_authenticator, TokenAuthentication):
            return True
        return has_staff_role(
            user,
            *getattr(view, "allowed_roles", ()),
        )


class IntegrationIdentityAccountSerializer(serializers.Serializer):
    username = serializers.CharField()
    token_created_at = serializers.DateTimeField(allow_null=True)
    scope_valid = serializers.BooleanField()


class IntegrationIdentityStatusSerializer(serializers.Serializer):
    id = serializers.CharField()
    label = serializers.CharField()
    permission = serializers.CharField()
    exists = serializers.BooleanField()
    accounts = IntegrationIdentityAccountSerializer(many=True)


class IntegrationIdentityUsernameSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, trim_whitespace=True)

    def validate_username(self, value):
        if not value:
            raise serializers.ValidationError("username cannot be blank")
        return value


class IntegrationIdentityCredentialSerializer(
    IntegrationIdentityAccountSerializer,
):
    identity_id = serializers.CharField()
    label = serializers.CharField()
    token = serializers.CharField()


class IntegrationIdentityViewSet(viewsets.ViewSet):
    permission_classes = [IntegrationIdentityConsolePermission]
    allowed_roles = {StaffRole.ADMIN}

    def _handoff(self, request, *, capability_id, action):
        return human_only_handoff(
            request,
            capability_id=capability_id,
            action=action,
            required_role="application_admin",
            detail=(
                "This action requires an accountable human to execute it in "
                "the staff application."
            ),
        )

    @extend_schema(
        responses={200: IntegrationIdentityStatusSerializer(many=True)},
    )
    def list(self, request):
        handoff = self._handoff(
            request,
            capability_id="integration_identities.list",
            action=(
                "Review integration identity credential status in a staff "
                "session."
            ),
        )
        if handoff:
            return handoff
        payload = [
            {
                "id": definition.id,
                "label": definition.label,
                "permission": definition.permission_name,
                "exists": bool(accounts),
                "accounts": [
                    {
                        "username": account.username,
                        "token_created_at": account.token_created_at,
                        "scope_valid": account.scope_valid,
                    }
                    for account in accounts
                ],
            }
            for definition, accounts
            in service_identities.integration_identity_statuses()
        ]
        return Response(
            IntegrationIdentityStatusSerializer(payload, many=True).data,
        )

    def _definition(self, identity_id):
        try:
            return service_identities.get_integration_identity_definition(
                identity_id,
            )
        except service_identities.IntegrationIdentityProvisionError as exc:
            raise Http404 from exc

    def _issue_credential(
        self,
        request,
        *,
        identity_id,
        rotate,
    ):
        capability_id = (
            "integration_identities.rotate"
            if rotate
            else "integration_identities.provision"
        )
        handoff = self._handoff(
            request,
            capability_id=capability_id,
            action=(
                "Rotate an integration identity token in a staff session."
                if rotate
                else "Provision an integration identity in a staff session."
            ),
        )
        if handoff:
            return handoff

        definition = self._definition(identity_id)
        serializer = IntegrationIdentityUsernameSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        try:
            provisioned = service_identities.provision_integration_identity(
                identity_id,
                username,
                rotate=rotate,
                require_existing_token=rotate,
            )
        except service_identities.IntegrationIdentityProvisionError as exc:
            raise serializers.ValidationError(
                {"username": [str(exc)]},
            ) from exc

        action_name = (
            "identity.integration_rotated"
            if rotate
            else "identity.integration_provisioned"
        )
        audit(
            action_name,
            target=f"{identity_id}:{username}",
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
            identity_id=identity_id,
            username=username,
            permission=definition.permission_name,
        )
        payload = {
            "identity_id": identity_id,
            "label": definition.label,
            "username": username,
            "token_created_at": provisioned.token.created,
            "scope_valid": service_identities.is_exact_service_identity_row(
                provisioned.user,
                definition.permission,
            ),
            "token": provisioned.token.key,
        }
        response_serializer = IntegrationIdentityCredentialSerializer(payload)
        return Response(
            response_serializer.data,
            status=(
                status.HTTP_200_OK
                if rotate
                else status.HTTP_201_CREATED
            ),
        )

    @extend_schema(
        request=IntegrationIdentityUsernameSerializer,
        responses={201: IntegrationIdentityCredentialSerializer},
        parameters=[IDENTITY_ID_PARAMETER],
    )
    @action(detail=True, methods=["post"])
    @transaction.atomic
    def provision(self, request, pk=None):
        return self._issue_credential(
            request,
            identity_id=pk,
            rotate=False,
        )

    @extend_schema(
        request=IntegrationIdentityUsernameSerializer,
        responses={200: IntegrationIdentityCredentialSerializer},
        parameters=[IDENTITY_ID_PARAMETER],
    )
    @action(detail=True, methods=["post"])
    @transaction.atomic
    def rotate(self, request, pk=None):
        return self._issue_credential(
            request,
            identity_id=pk,
            rotate=True,
        )


def register(router):
    router.register(
        "integration-identities",
        IntegrationIdentityViewSet,
        basename="integration-identity",
    )
