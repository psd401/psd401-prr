# ABOUTME: PostgreSQL contract coverage for case-workspace JSON correlations.
# ABOUTME: Production JSONB comparisons must stay typed and fail closed.
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import StaffRole, apply_staff_roles
from apps.correspondence.models import (
    Correspondence,
    CorrespondenceDelivery,
    CorrespondenceDeliveryAttempt,
)
from apps.records.models import Installment, PublicRecordsRequest, Requester


@skipUnless(
    connection.vendor == "postgresql",
    "Case-workspace JSON contract requires PostgreSQL.",
)
@override_settings(PRR_LOCAL_IMPLICIT_STAFF_ROLES=())
class CaseWorkspacePostgresContractTests(TestCase):
    def test_closure_readiness_correlates_json_installment_id_without_type_error(self):
        officer = get_user_model().objects.create_user(
            "postgres-case-officer",
            email="postgres-case-officer@psd401.net",
            is_staff=True,
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(
                email="postgres-case-requester@example.org",
            ),
            description="PostgreSQL closure-readiness contract.",
        )
        now = timezone.now()
        installment = Installment.objects.create(
            request=prr,
            sequence=1,
            released_at=now,
        )
        notice = Correspondence.objects.create(
            request=prr,
            direction=Correspondence.Direction.OUTBOUND,
            kind=Correspondence.Kind.INSTALLMENT_NOTICE,
            status=Correspondence.Status.SENT,
            approved_subject="Records installment available",
            approved_body="Use the approved secure download link.",
            approved_metadata={"installment_id": installment.pk},
            approved_by=officer,
            approved_at=now,
            queued_by=officer,
            queued_at=now,
            sent_at=now,
        )
        delivery = CorrespondenceDelivery.objects.create(
            correspondence=notice,
            status=CorrespondenceDelivery.Status.SENT,
            idempotency_key="postgres-case-workspace-notice",
            attempt_count=1,
            successful_attempt_number=1,
            provider_message_id="postgres-provider-message",
            completed_at=now,
        )
        CorrespondenceDeliveryAttempt.objects.create(
            delivery=delivery,
            attempt_number=1,
            claim_owner="postgres-contract-worker",
            claimed_at=now,
            result_at=now,
            result_category=CorrespondenceDeliveryAttempt.ResultCategory.SENT,
            provider_message_id="postgres-provider-message",
            provider_thread_id="postgres-provider-thread",
            provider_timestamp=now,
            result_fingerprint="e" * 64,
            actor=officer,
        )
        self.client.force_login(officer)

        response = self.client.get(
            f"/api/staff/requests/{prr.number}/closure-readiness/"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            response.json()["counts"]["undelivered_release_notices"],
            0,
        )
