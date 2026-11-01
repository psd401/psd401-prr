# ABOUTME: Application-admin CRUD for the district's own counsel email domains,
# ABOUTME: which only tag records into the privilege queue for a human to read.
"""The administrator-managed district-counsel domain list.

Privilege attaches to communications with the district's *own* lawyers, so
this list holds only those firms. It is seeded empty: nobody but an
application administrator knows which firms they are, and an empty list tags
nothing. A plaintiff's firm needs no entry, because the deterministic
``external_sender`` tag and the sender-domain filter already separate it.

An entry has exactly one effect: records touching that domain carry the
``counsel_domain`` tag and appear in the privilege queue so a human looks at
them before anything ships. It withholds nothing and redacts nothing — the
withheld path still requires a named reviewer, an ExemptionCitation, and
second review.
"""

import re

from rest_framework import serializers, viewsets

from apps.api.identity import StaffRole
from apps.audit.models import audit
from apps.records.models import CounselDomain

#: An exact DNS domain, the same shape the settings allowlists accept. A
#: pattern rather than free text so one mistyped entry cannot widen the tag to
#: everything.
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    flags=re.ASCII,
)


class CounselDomainSerializer(serializers.ModelSerializer):
    class Meta:
        model = CounselDomain
        fields = ["id", "domain", "note", "active", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_domain(self, value):
        domain = str(value or "").strip().lower().lstrip("@")
        if _DOMAIN.fullmatch(domain) is None:
            raise serializers.ValidationError(
                "Enter one exact email domain, such as lawfirm.com."
            )
        return domain


class CounselDomainViewSet(viewsets.ModelViewSet):
    queryset = CounselDomain.objects.order_by("domain")
    serializer_class = CounselDomainSerializer
    allowed_roles = {StaffRole.ADMIN}
    allow_readonly_agent = False

    def perform_create(self, serializer):
        instance = serializer.save()
        audit(
            "records.counsel_domain_created",
            target=str(instance.pk),
            actor=self.request.user,
            domain=instance.domain,
            active=instance.active,
        )

    def perform_update(self, serializer):
        before = {
            "domain": serializer.instance.domain,
            "note": serializer.instance.note,
            "active": serializer.instance.active,
        }
        instance = serializer.save()
        audit(
            "records.counsel_domain_updated",
            target=str(instance.pk),
            actor=self.request.user,
            before=before,
            domain=instance.domain,
            note=instance.note,
            active=instance.active,
        )

    def perform_destroy(self, instance):
        detail = {"domain": instance.domain, "active": instance.active}
        target = str(instance.pk)
        instance.delete()
        audit(
            "records.counsel_domain_deleted",
            target=target,
            actor=self.request.user,
            **detail,
        )


def register(router):
    router.register(
        "counsel-domains",
        CounselDomainViewSet,
        basename="counsel-domain",
    )
