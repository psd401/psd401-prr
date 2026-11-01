# ABOUTME: Session-only lifecycle API for named personal access tokens.
# ABOUTME: Secrets are hashed at rest and returned only on create or rotate.
import hashlib
import math
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.authentication import TokenAuthentication
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import audit
from apps.configuration import runtime

from .authentication import (
    PersonalAccessTokenAuthentication,
    StaffSessionAuthentication,
)
from .handoffs import human_only_handoff
from .models import PersonalAccessToken
from .permissions import IsStaffOrReadOnlyAgent
from .throttles import PersonalAccessTokenCreateThrottle


class PersonalAccessTokenPermission(BasePermission):
    """Expose token callers to an explicit handoff; require staff sessions."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if isinstance(request.successful_authenticator, TokenAuthentication):
            return True
        return IsStaffOrReadOnlyAgent().has_permission(request, view)


class PersonalAccessTokenMetadataSerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonalAccessToken
        fields = (
            "id",
            "name",
            "prefix",
            "created_at",
            "expires_at",
            "last_used_at",
            "revoked_at",
        )
        read_only_fields = fields


class PersonalAccessTokenCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, trim_whitespace=True)
    lifetime_days = serializers.IntegerField(min_value=1)

    def validate_name(self, value):
        if not value:
            raise serializers.ValidationError("Give this token a name.")
        return value

    def validate_lifetime_days(self, value):
        maximum = runtime.get("PRR_PERSONAL_TOKEN_MAX_LIFETIME_DAYS")
        if value > maximum:
            raise serializers.ValidationError(
                f"Must not exceed {maximum} days.",
            )
        return value


class PersonalAccessTokenValueSerializer(
    PersonalAccessTokenMetadataSerializer,
):
    token = serializers.CharField()

    class Meta(PersonalAccessTokenMetadataSerializer.Meta):
        fields = (*PersonalAccessTokenMetadataSerializer.Meta.fields, "token")


def _response_payload(token, secret):
    payload = PersonalAccessTokenMetadataSerializer(token).data
    payload["token"] = secret
    return payload


def _create_token(*, user, name, lifetime_days, now):
    expires_at = now + timedelta(days=lifetime_days)
    for _attempt in range(5):
        prefix = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        presented = f"prr_pat_{prefix}_{secret}"
        token_hash = hashlib.sha256(presented.encode()).hexdigest()
        try:
            with transaction.atomic():
                token = PersonalAccessToken.objects.create(
                    user=user,
                    name=name,
                    prefix=prefix,
                    token_hash=token_hash,
                    expires_at=expires_at,
                )
            return token, presented
        except IntegrityError:
            if PersonalAccessToken.objects.filter(
                user=user,
                name__iexact=name,
                revoked_at__isnull=True,
            ).exists():
                raise serializers.ValidationError(
                    {"name": ["You already have a non-revoked token with this name."]},
                ) from None
    raise serializers.ValidationError(
        {"non_field_errors": ["A unique token could not be generated. Try again."]},
    )


def _ensure_active_capacity(*, user, now, replacing=None):
    active = PersonalAccessToken.objects.filter(
        user=user,
        revoked_at__isnull=True,
        expires_at__gt=now,
    )
    if replacing is not None:
        active = active.exclude(pk=replacing.pk)
    maximum = runtime.get("PRR_PERSONAL_TOKEN_MAX_ACTIVE")
    if active.count() >= maximum:
        raise serializers.ValidationError(
            {
                "non_field_errors": [
                    f"You already have {maximum} active tokens.",
                ],
            },
        )


class PersonalAccessTokenLifecycleView(APIView):
    authentication_classes = [
        StaffSessionAuthentication,
        PersonalAccessTokenAuthentication,
        TokenAuthentication,
    ]
    permission_classes = [PersonalAccessTokenPermission]

    def handoff(self, request, *, capability_id, action):
        return human_only_handoff(
            request,
            capability_id=capability_id,
            action=action,
            required_role="role_mapped_staff",
        )


class PersonalAccessTokenCollectionView(PersonalAccessTokenLifecycleView):
    def get_throttles(self):
        if self.request.method == "POST":
            return [PersonalAccessTokenCreateThrottle()]
        return []

    @extend_schema(
        responses={200: PersonalAccessTokenMetadataSerializer(many=True)},
    )
    def get(self, request):
        handoff = self.handoff(
            request,
            capability_id="api_tokens.list",
            action="Review personal access token metadata in a staff session.",
        )
        if handoff:
            return handoff
        tokens = PersonalAccessToken.objects.filter(user=request.user)
        return Response(PersonalAccessTokenMetadataSerializer(tokens, many=True).data)

    @extend_schema(
        request=PersonalAccessTokenCreateSerializer,
        responses={201: PersonalAccessTokenValueSerializer},
    )
    @transaction.atomic
    def post(self, request):
        handoff = self.handoff(
            request,
            capability_id="api_tokens.create",
            action="Create a personal access token in a staff session.",
        )
        if handoff:
            return handoff
        serializer = PersonalAccessTokenCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        name = serializer.validated_data["name"]
        now = timezone.now()
        if PersonalAccessToken.objects.filter(
            user=user,
            name__iexact=name,
            revoked_at__isnull=True,
        ).exists():
            raise serializers.ValidationError(
                {"name": ["You already have a non-revoked token with this name."]},
            )
        _ensure_active_capacity(user=user, now=now)
        token, presented = _create_token(
            user=user,
            name=name,
            lifetime_days=serializer.validated_data["lifetime_days"],
            now=now,
        )
        audit(
            "identity.personal_api_token_created",
            target=str(user.pk),
            actor=user,
            ip_address=request.META.get("REMOTE_ADDR"),
            name=token.name,
            prefix=token.prefix,
        )
        return Response(
            _response_payload(token, presented),
            status=status.HTTP_201_CREATED,
        )


class PersonalAccessTokenRotateView(PersonalAccessTokenLifecycleView):
    @extend_schema(
        request=None,
        responses={
            200: PersonalAccessTokenValueSerializer,
            404: OpenApiResponse(description="Token not found."),
        },
    )
    @transaction.atomic
    def post(self, request, token_id):
        handoff = self.handoff(
            request,
            capability_id="api_tokens.rotate",
            action="Rotate a personal access token in a staff session.",
        )
        if handoff:
            return handoff
        user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        previous = get_object_or_404(
            PersonalAccessToken.objects.select_for_update(),
            pk=token_id,
            user=user,
        )
        if previous.revoked_at is not None:
            raise serializers.ValidationError(
                {"detail": "A revoked token cannot be rotated."},
            )
        now = timezone.now()
        _ensure_active_capacity(user=user, now=now, replacing=previous)
        original_days = max(
            1,
            math.ceil(
                (previous.expires_at - previous.created_at).total_seconds()
                / 86400
            ),
        )
        lifetime_days = min(
            original_days,
            runtime.get("PRR_PERSONAL_TOKEN_MAX_LIFETIME_DAYS"),
        )
        previous.revoked_at = now
        previous.save(update_fields=["revoked_at"])
        token, presented = _create_token(
            user=user,
            name=previous.name,
            lifetime_days=lifetime_days,
            now=now,
        )
        audit(
            "identity.personal_api_token_rotated",
            target=str(user.pk),
            actor=user,
            ip_address=request.META.get("REMOTE_ADDR"),
            name=token.name,
            prefix=token.prefix,
            replaced_prefix=previous.prefix,
        )
        return Response(_response_payload(token, presented))


class PersonalAccessTokenDetailView(PersonalAccessTokenLifecycleView):
    @extend_schema(
        responses={
            204: None,
            404: OpenApiResponse(description="Token not found."),
        },
    )
    @transaction.atomic
    def delete(self, request, token_id):
        handoff = self.handoff(
            request,
            capability_id="api_tokens.revoke",
            action="Revoke a personal access token in a staff session.",
        )
        if handoff:
            return handoff
        token = get_object_or_404(
            PersonalAccessToken.objects.select_for_update(),
            pk=token_id,
            user=request.user,
        )
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])
            audit(
                "identity.personal_api_token_revoked",
                target=str(request.user.pk),
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
                name=token.name,
                prefix=token.prefix,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
