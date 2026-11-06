# ABOUTME: Application-admin read and reasoned write surface for the in-app
# ABOUTME: settings store, plus each setting's full change history.
"""The staff API behind the Settings page.

Read is a merge of the typed registry and the resolved values, so the page
can show what a switch does and what it would cost before anyone changes it.
Write is one atomic primitive per setting: value plus reason, going through
``apps.configuration.services.set_value`` -- the same door the district's
accountability story depends on. There is no bulk write, and no path that
skips the reason.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from apps.api.handoffs import human_only_handoff
from apps.api.identity import StaffRole
from apps.configuration import runtime, verification
from apps.configuration import values as setting_values
from apps.configuration.models import AppSecret, AppSettingChange
from apps.configuration.registry import (
    CATEGORY_ORDER,
    SETTINGS,
    SETTINGS_BY_KEY,
    dependents_of,
)
from apps.configuration.services import (
    REASON_MAX_LENGTH,
    REASON_MIN_LENGTH,
    set_secret,
    set_value,
)


class AppSettingChangeSerializer(serializers.ModelSerializer):
    actor_email = serializers.SerializerMethodField()
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AppSettingChange
        fields = [
            "id",
            "at",
            "key",
            "old_value",
            "new_value",
            "reason",
            "source",
            "actor_email",
            "actor_name",
        ]

    def get_actor_email(self, obj) -> str:
        return obj.actor.email if obj.actor else ""

    def get_actor_name(self, obj) -> str:
        if not obj.actor:
            return ""
        return obj.actor.get_full_name() or obj.actor.get_username()


class AppSettingSerializer(serializers.Serializer):
    """The shape of one row: the registry merged with its resolved value.

    ``value`` and ``default`` are whatever the setting's type makes them --
    a boolean, a whole number, a string, or a list of strings -- so the page
    is told the type and the bounds and renders the editor that fits. The
    bounds travel with the row for one reason: an administrator should see
    the limit before they type past it, not after.
    """

    key = serializers.CharField()
    label = serializers.CharField()
    type = serializers.CharField()
    category = serializers.CharField()
    consequence = serializers.CharField()
    value = serializers.JSONField()
    default = serializers.JSONField()
    unit = serializers.CharField(allow_blank=True)
    minimum = serializers.IntegerField(allow_null=True)
    maximum = serializers.IntegerField(allow_null=True)
    periods = serializers.ListField(child=serializers.CharField())
    max_length = serializers.IntegerField(allow_null=True)
    max_items = serializers.IntegerField(allow_null=True)
    allow_blank = serializers.BooleanField()
    choices = serializers.ListField(child=serializers.CharField())
    suggestions = serializers.ListField(child=serializers.CharField())
    requires = serializers.ListField(child=serializers.CharField())
    required_by = serializers.ListField(child=serializers.CharField())
    last_change = AppSettingChangeSerializer(allow_null=True)
    verification = serializers.JSONField(allow_null=True)
    #: For a trust setting that can act without a person: how the live model
    #: scored on its latest evaluation, so the evidence sits beside the switch.
    evaluation = serializers.JSONField(allow_null=True)


class AppSettingPageSerializer(serializers.Serializer):
    categories = serializers.ListField(child=serializers.CharField())
    reason_min_length = serializers.IntegerField()
    results = AppSettingSerializer(many=True)


class AppSettingHistorySerializer(serializers.Serializer):
    key = serializers.CharField()
    results = AppSettingChangeSerializer(many=True)


class AppSettingUpdateSerializer(serializers.Serializer):
    """Value plus reason.

    ``value`` is deliberately untyped here. The registry is the one place a
    setting's type and bounds are written down, and
    ``apps.configuration.services.set_value`` is the one door that enforces
    them; a second, looser copy of the rules in a serializer field is how the
    two drift. What this serializer does own is the reason, because the
    reason is the same for every setting.
    """

    value = serializers.JSONField()
    reason = serializers.CharField(
        min_length=REASON_MIN_LENGTH,
        max_length=REASON_MAX_LENGTH,
        trim_whitespace=True,
    )


def _bound(spec, attribute):
    """A numeric bound, or null for a setting the bound does not describe."""
    return getattr(spec, attribute) if spec.type == setting_values.INTEGER else None


def _secret_state(spec, *, secrets_by_key):
    """What a write-only setting is allowed to say about itself.

    Whether one is installed, a fingerprint an administrator can compare
    against the value they hold, and who installed it. Never the value, and
    never anything from which the value could be recovered.
    """
    row = secrets_by_key.get(spec.key)
    if row is None or not row.ciphertext:
        return {"set": False, "fingerprint": "", "set_by": "", "set_at": None}
    return {
        "set": True,
        "fingerprint": row.fingerprint,
        "set_by": (
            (row.set_by.get_full_name() or row.set_by.get_username())
            if row.set_by
            else "System import"
        ),
        "set_at": row.updated_at,
    }


def _serialize(spec, *, values, last_change_by_key, secrets_by_key=None, checks=None):
    last_change = last_change_by_key.get(spec.key)
    shaped = spec.type in {
        setting_values.TEXT,
        setting_values.EMAIL,
        setting_values.DOMAIN,
        setting_values.SECRET,
    }
    listed = spec.type in setting_values.LIST_TYPES
    secret = spec.type == setting_values.SECRET
    return {
        "key": spec.key,
        "label": spec.label,
        "type": spec.type,
        "category": spec.category,
        "consequence": spec.consequence,
        "value": (
            _secret_state(spec, secrets_by_key=secrets_by_key or {})
            if secret
            else setting_values.to_json(spec, values[spec.key])
        ),
        "default": (
            None
            if secret
            else setting_values.to_json(
                spec, setting_values.coerce(spec, spec.default)
            )
        ),
        "unit": spec.unit,
        "minimum": _bound(spec, "minimum"),
        "maximum": _bound(spec, "maximum"),
        "periods": list(spec.periods or setting_values.RATE_PERIODS)
        if spec.type == setting_values.RATE
        else [],
        "max_length": spec.max_length if shaped else None,
        "max_items": spec.max_items if listed else None,
        "allow_blank": spec.allow_blank,
        "choices": list(spec.choices),
        "suggestions": list(spec.suggestions),
        "requires": list(spec.requires),
        "required_by": list(dependents_of(spec.key)),
        "last_change": (
            AppSettingChangeSerializer(last_change).data if last_change else None
        ),
        "verification": (checks or {}).get(spec.key),
        "evaluation": _evaluation_for(spec.key),
    }


def _evaluation_for(key: str):
    """The intake trust rows show "not a request" precision and recall (#34)."""
    from apps.records.advisory_ai import model_for_kind
    from apps.records.evaluation import latest_not_request_metrics
    from apps.records.intake_trust import AUTO_LABEL_SETTINGS
    from apps.records.models import AdvisoryRun

    if key not in AUTO_LABEL_SETTINGS.values():
        return None
    return latest_not_request_metrics(
        model_for_kind(AdvisoryRun.Kind.INTAKE_CLASSIFICATION),
    )


class AppSettingViewSet(viewsets.ViewSet):
    """One row per registered setting, addressed by its key."""

    allowed_roles = {StaffRole.ADMIN}
    allow_readonly_agent = False
    lookup_field = "key"
    lookup_value_regex = "[A-Z][A-Z0-9_]*"
    serializer_class = AppSettingSerializer

    def _last_change_for(self, keys):
        """The newest change per key, in one query.

        ``AppSettingChange`` orders newest first, so the first row seen for a
        key is its latest.
        """
        latest: dict[str, AppSettingChange] = {}
        rows = AppSettingChange.objects.filter(key__in=keys).select_related(
            "actor"
        )
        for row in rows:
            latest.setdefault(row.key, row)
        return latest

    def _secrets_for(self, keys):
        """The value-free description of each stored secret, in one query."""
        return {
            row.key: row
            for row in AppSecret.objects.filter(key__in=keys).select_related(
                "set_by"
            )
        }

    @extend_schema(responses={200: AppSettingPageSerializer})
    def list(self, request):
        values = runtime.all_values()
        all_keys = [spec.key for spec in SETTINGS]
        last_change_by_key = self._last_change_for(all_keys)
        secrets_by_key = self._secrets_for(all_keys)
        checks = verification.state_for(all_keys)
        return Response(
            {
                "categories": list(CATEGORY_ORDER),
                "reason_min_length": REASON_MIN_LENGTH,
                "results": [
                    _serialize(
                        spec,
                        values=values,
                        last_change_by_key=last_change_by_key,
                        secrets_by_key=secrets_by_key,
                        checks=checks,
                    )
                    for spec in sorted(
                        SETTINGS,
                        key=lambda item: (
                            CATEGORY_ORDER.index(item.category),
                            item.label,
                        ),
                    )
                ],
            }
        )

    def _spec_or_404(self, key):
        spec = SETTINGS_BY_KEY.get(key)
        if spec is None:
            raise NotFound("That setting is not managed in the application.")
        return spec

    @extend_schema(
        request=AppSettingUpdateSerializer,
        responses={200: AppSettingSerializer},
    )
    def partial_update(self, request, key=None):
        handoff = human_only_handoff(
            request,
            capability_id="app_settings.update",
            action="Change a district setting.",
            required_role=StaffRole.ADMIN,
            detail=(
                "Changing a district setting requires an Application Admin "
                "to make the change in the staff application."
            ),
        )
        if handoff:
            return handoff
        spec = self._spec_or_404(key)
        payload = AppSettingUpdateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        # One endpoint for every setting, including the write-only ones: the
        # difference is which door the value goes through, not how a client
        # asks. A secret still leaves by no door at all -- the response below
        # describes it rather than returning it.
        write = set_secret if spec.type == setting_values.SECRET else set_value
        try:
            write(
                spec.key,
                payload.validated_data["value"],
                actor=request.user,
                reason=payload.validated_data["reason"],
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except DjangoValidationError as exc:
            # These refusals are about the change, not about the shape of the
            # value: a missing parent switch, an absent prerequisite, a no-op.
            # Reporting them as a field error on "value" would tell a client
            # to go and fix the boolean it sent.
            raise ValidationError(
                {"detail": " ".join(exc.messages)}
            ) from exc
        return Response(
            _serialize(
                spec,
                values=(
                    {}
                    if spec.type == setting_values.SECRET
                    else {spec.key: runtime.get(spec.key)}
                ),
                last_change_by_key=self._last_change_for([spec.key]),
                secrets_by_key=self._secrets_for([spec.key]),
                checks=verification.state_for([spec.key]),
            ),
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses={200: AppSettingHistorySerializer})
    @action(detail=True, methods=["get"])
    def history(self, request, key=None):
        spec = self._spec_or_404(key)
        rows = AppSettingChange.objects.filter(key=spec.key).select_related(
            "actor"
        )
        return Response(
            {
                "key": spec.key,
                "results": AppSettingChangeSerializer(rows, many=True).data,
            }
        )


def register(router):
    router.register("settings", AppSettingViewSet, basename="app-setting")
