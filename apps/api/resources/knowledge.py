# ABOUTME: Records-Officer-only CRUD for the district knowledge pack: role
# ABOUTME: accounts, noise senders, and per-system search notes, each audited.
"""The district knowledge pack (#35).

What a Records Officer knows and the intent advisory otherwise cannot: who
held a role and when, which senders are district mass mail, and how each
system is actually searched. It is advisory input only
(``advisory_ai._build_advisory_input``): nothing here routes a request, places
a hold, starts a collection, or reaches a requester.

Every write is audited with before/after snapshots. The pack is Records
Officer only in both directions -- it names staff accounts, and editing it
steers what the advisory proposes -- so the read-only agent identity cannot
read it either.
"""

import re

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction
from rest_framework import serializers, viewsets

from apps.api.identity import StaffRole
from apps.audit.models import audit
from apps.records.models import (
    KnowledgeNoiseSender,
    KnowledgeRoleAccount,
    KnowledgeSystemNote,
    RecordSystem,
)

#: The same exact-DNS-domain shape the counsel-domain list accepts.
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    flags=re.ASCII,
)


def _is_email(value: str) -> bool:
    try:
        validate_email(value)
    except DjangoValidationError:
        return False
    return True


def _required_text(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise serializers.ValidationError(f"Enter the {label}.")
    return text


class KnowledgeRoleAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeRoleAccount
        fields = [
            "id",
            "role",
            "account_email",
            "starts_on",
            "ends_on",
            "note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_role(self, value):
        return _required_text(value, "role")

    def validate_account_email(self, value):
        return str(value).strip().lower()

    def validate(self, attrs):
        instance = self.instance
        starts_on = attrs.get("starts_on", getattr(instance, "starts_on", None))
        ends_on = attrs.get("ends_on", getattr(instance, "ends_on", None))
        if starts_on and ends_on and ends_on < starts_on:
            raise serializers.ValidationError(
                {"ends_on": "The end date cannot be before the start date."}
            )
        return attrs


class KnowledgeNoiseSenderSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeNoiseSender
        fields = ["id", "kind", "value", "note", "active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]
        # Uniqueness is checked in validate() on the normalized value.
        validators = []

    def validate(self, attrs):
        kind = attrs.get("kind", getattr(self.instance, "kind", ""))
        raw = attrs.get("value", getattr(self.instance, "value", ""))
        value = str(raw or "").strip().lower()
        if kind == KnowledgeNoiseSender.Kind.DOMAIN:
            value = value.lstrip("@")
            valid = _DOMAIN.fullmatch(value) is not None
            message = "Enter one exact sender domain, such as news.example.org."
        elif kind == KnowledgeNoiseSender.Kind.ADDRESS:
            valid = _is_email(value)
            message = "Enter one sender email address."
        else:
            # A list is named by its posting address or its List-Id, which is
            # domain-shaped.
            valid = _is_email(value) or _DOMAIN.fullmatch(value) is not None
            message = "Enter the list's email address or its list ID."
        if not valid:
            raise serializers.ValidationError({"value": message})
        attrs["value"] = value
        # Checked on the normalized value, so "NEWS.example.org" cannot
        # duplicate an entry.
        duplicate = KnowledgeNoiseSender.objects.filter(kind=kind, value=value)
        if self.instance is not None:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError({"value": "That sender is already on the list."})
        return attrs


class KnowledgeSystemNoteSerializer(serializers.ModelSerializer):
    record_system_id = serializers.PrimaryKeyRelatedField(
        source="record_system",
        queryset=RecordSystem.objects.all(),
    )
    record_system_slug = serializers.CharField(source="record_system.slug", read_only=True)
    record_system_name = serializers.CharField(source="record_system.name", read_only=True)

    class Meta:
        model = KnowledgeSystemNote
        fields = [
            "id",
            "record_system_id",
            "record_system_slug",
            "record_system_name",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_record_system_id(self, value):
        if self.instance is None and KnowledgeSystemNote.objects.filter(record_system=value).exists():
            raise serializers.ValidationError(
                "That system already has search notes; edit them instead."
            )
        return value

    def validate_notes(self, value):
        return _required_text(value, "search notes")

    def update(self, instance, validated_data):
        # A note belongs to the system it was written for.
        validated_data.pop("record_system", None)
        return super().update(instance, validated_data)


def _role_account_snapshot(row: KnowledgeRoleAccount) -> dict:
    return {
        "role": row.role,
        "account_email": row.account_email,
        "starts_on": row.starts_on.isoformat() if row.starts_on else None,
        "ends_on": row.ends_on.isoformat() if row.ends_on else None,
        "note": row.note,
    }


def _noise_sender_snapshot(row: KnowledgeNoiseSender) -> dict:
    return {"kind": row.kind, "value": row.value, "note": row.note, "active": row.active}


def _system_note_snapshot(row: KnowledgeSystemNote) -> dict:
    return {"record_system": row.record_system.slug, "notes": row.notes}


class _AuditedKnowledgeViewSet(viewsets.ModelViewSet):
    """Create, update, and delete each write one audit row in the same
    transaction, named ``knowledge.<entity>_<created|updated|deleted>``."""

    allowed_roles = {StaffRole.RECORDS_OFFICER}
    allow_readonly_agent = False
    audit_entity = ""

    def snapshot(self, instance) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def _audit(self, verb: str, target, **detail):
        audit(
            f"knowledge.{self.audit_entity}_{verb}",
            target=str(target),
            actor=self.request.user,
            **detail,
        )

    def perform_create(self, serializer):
        with transaction.atomic():
            instance = serializer.save()
            self._audit("created", instance.pk, after=self.snapshot(instance))

    def perform_update(self, serializer):
        with transaction.atomic():
            before = self.snapshot(serializer.instance)
            instance = serializer.save()
            self._audit("updated", instance.pk, before=before, after=self.snapshot(instance))

    def perform_destroy(self, instance):
        with transaction.atomic():
            before = self.snapshot(instance)
            target = instance.pk
            instance.delete()
            self._audit("deleted", target, before=before)


class KnowledgeRoleAccountViewSet(_AuditedKnowledgeViewSet):
    queryset = KnowledgeRoleAccount.objects.all()
    serializer_class = KnowledgeRoleAccountSerializer
    audit_entity = "role_account"

    def snapshot(self, instance):
        return _role_account_snapshot(instance)


class KnowledgeNoiseSenderViewSet(_AuditedKnowledgeViewSet):
    queryset = KnowledgeNoiseSender.objects.all()
    serializer_class = KnowledgeNoiseSenderSerializer
    audit_entity = "noise_sender"

    def snapshot(self, instance):
        return _noise_sender_snapshot(instance)


class KnowledgeSystemNoteViewSet(_AuditedKnowledgeViewSet):
    queryset = KnowledgeSystemNote.objects.select_related("record_system")
    serializer_class = KnowledgeSystemNoteSerializer
    audit_entity = "system_note"

    def snapshot(self, instance):
        return _system_note_snapshot(instance)


def register(router):
    router.register(
        "knowledge/role-accounts",
        KnowledgeRoleAccountViewSet,
        basename="knowledge-role-account",
    )
    router.register(
        "knowledge/noise-senders",
        KnowledgeNoiseSenderViewSet,
        basename="knowledge-noise-sender",
    )
    router.register(
        "knowledge/system-notes",
        KnowledgeSystemNoteViewSet,
        basename="knowledge-system-note",
    )
