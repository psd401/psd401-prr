"""Installment packages include only verified, legally releasable bytes."""

import hashlib
import io
import json
import zipfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence import services as correspondence_services
from apps.correspondence.models import Correspondence
from apps.records.exemption_logs import generate_exemption_log
from apps.records.models import (
    ExemptionCitation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
)
from apps.records.production import (
    prepare_installment,
    release_installment,
    release_installment_with_notice,
)

from .object_store_fakes import FakeObjectClient


@override_settings(OBJECT_STORE={"bucket": "records"})
class InstallmentProductionTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer",
            "officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        requester = Requester.objects.create(email="requester@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Board records.",
        )
        self.payload = b"%PDF-1.7\nresponsive record"
        self.item = RecordItem.objects.create(
            request=self.prr,
            original_filename="../Board Minutes (final).pdf",
            storage_key=f"records/{self.prr.id}/original.pdf",
            sha256=hashlib.sha256(self.payload).hexdigest(),
            content_length=len(self.payload),
            content_type="application/pdf",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )
        self.installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
        )
        self.installment.items.add(self.item)

    def test_prepares_verified_zip_manifest_and_chain_of_custody(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})

        result = prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )

        self.installment.refresh_from_db()
        self.assertEqual(result.pk, self.installment.pk)
        self.assertTrue(self.installment.package_storage_key.startswith(
            f"productions/{self.prr.id}/{self.installment.pk}/"
        ))
        self.assertRegex(self.installment.package_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            self.installment.package_size,
            len(client.objects[self.installment.package_storage_key]),
        )
        self.assertIsNotNone(self.installment.package_prepared_at)
        self.assertEqual(self.installment.package_prepared_by, self.officer)
        self.assertEqual(len(client.put_calls), 1)
        self.assertEqual(client.put_calls[0]["IfNoneMatch"], "*")

        with zipfile.ZipFile(
            io.BytesIO(client.objects[self.installment.package_storage_key])
        ) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), 2)
            record_name = next(name for name in names if name.startswith("records/"))
            self.assertNotIn("..", record_name)
            self.assertEqual(archive.read(record_name), self.payload)
            manifest = json.loads(archive.read("manifest.json"))

        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["request_number"], self.prr.number)
        self.assertEqual(manifest["installment_sequence"], 1)
        self.assertEqual(manifest["records"][0]["record_item_id"], str(self.item.pk))
        self.assertEqual(manifest["records"][0]["sha256"], self.item.sha256)
        self.assertNotIn("storage_key", json.dumps(manifest))
        self.assertNotIn("requester@example.org", json.dumps(manifest))
        self.assertTrue(AuditLog.objects.filter(
            action="installment.prepared",
            target=self.prr.number,
            actor=self.officer,
            detail__package_sha256=self.installment.package_sha256,
        ).exists())

    def test_production_lock_does_not_join_nullable_withholding_exemption(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})

        with CaptureQueriesContext(connection) as queries:
            prepare_installment(
                self.installment.pk,
                actor=self.officer,
                _client=client,
            )

        lock_query = next(
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "records_recorditem"' in query["sql"]
        )
        self.assertNotIn(
            'LEFT OUTER JOIN "records_exemptioncitation"',
            lock_query,
        )

    def test_retry_of_unchanged_prepared_installment_is_idempotent(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})
        first = prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )

        second = prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )

        self.assertEqual(second.package_storage_key, first.package_storage_key)
        self.assertEqual(len(client.put_calls), 1)
        self.assertEqual(
            AuditLog.objects.filter(action="installment.prepared").count(),
            1,
        )

    def test_changed_installment_cannot_reuse_a_prepared_package(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        second_payload = b"another record"
        second_item = RecordItem.objects.create(
            request=self.prr,
            original_filename="second.txt",
            storage_key=f"records/{self.prr.id}/second.txt",
            sha256=hashlib.sha256(second_payload).hexdigest(),
            content_length=len(second_payload),
            content_type="text/plain",
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.officer,
            reviewed_at=timezone.now(),
        )
        self.installment.items.add(second_item)

        with self.assertRaisesMessage(ValidationError, "changed after preparation"):
            prepare_installment(
                self.installment.pk,
                actor=self.officer,
                _client=client,
            )

        self.assertEqual(len(client.put_calls), 1)

    def test_release_revalidates_review_state_against_prepared_manifest(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        self.item.review_state = RecordItem.ReviewState.NON_RESPONSIVE
        self.item.save(update_fields=["review_state"])

        with self.assertRaisesMessage(ValidationError, "not releasable"):
            release_installment(
                self.installment.pk,
                actor=self.officer,
            )

        self.installment.refresh_from_db()
        self.assertIsNone(self.installment.released_at)

    def test_redacted_record_can_never_fall_back_to_original_bytes(self):
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)",
            short_name="Personal information",
        )
        Redaction.objects.create(
            record_item=self.item,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption=exemption,
            created_by=self.officer,
        )
        client = FakeObjectClient({self.item.storage_key: self.payload})

        with self.assertRaisesMessage(
            ValidationError,
            "approved redacted artifact",
        ):
            prepare_installment(
                self.installment.pk,
                actor=self.officer,
                _client=client,
            )

        self.assertEqual(client.get_calls, [])
        self.assertEqual(client.put_calls, [])

    def test_required_second_review_blocks_packaging_before_storage_access(self):
        exemption = ExemptionCitation.objects.create(
            citation="20 U.S.C. § 1232g",
            short_name="FERPA education records",
            requires_second_review=True,
        )
        Redaction.objects.create(
            record_item=self.item,
            page_number=1,
            x0=0.1,
            y0=0.1,
            x1=0.2,
            y1=0.2,
            exemption=exemption,
            created_by=self.officer,
        )
        client = FakeObjectClient({self.item.storage_key: self.payload})

        with self.assertRaisesMessage(ValidationError, "second review"):
            prepare_installment(
                self.installment.pk,
                actor=self.officer,
                _client=client,
            )

        self.assertEqual(client.get_calls, [])
        self.assertEqual(client.put_calls, [])

    def test_nonresponsive_and_duplicate_items_are_rejected(self):
        for state in (
            RecordItem.ReviewState.UNREVIEWED,
            RecordItem.ReviewState.NON_RESPONSIVE,
            RecordItem.ReviewState.DUPLICATE,
        ):
            with self.subTest(state=state):
                self.item.review_state = state
                self.item.save(update_fields=["review_state"])
                with self.assertRaisesMessage(
                    ValidationError,
                    "not releasable",
                ):
                    prepare_installment(
                        self.installment.pk,
                        actor=self.officer,
                        _client=FakeObjectClient({self.item.storage_key: self.payload}),
                    )

    def test_item_from_another_request_is_rejected_before_any_storage_read(self):
        other_request = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="other@example.org"),
            description="Other request.",
        )
        self.item.request = other_request
        self.item.save(update_fields=["request"])
        client = FakeObjectClient({self.item.storage_key: self.payload})

        with self.assertRaisesMessage(ValidationError, "same request"):
            prepare_installment(
                self.installment.pk,
                actor=self.officer,
                _client=client,
            )

        self.assertEqual(client.get_calls, [])
        self.assertEqual(client.put_calls, [])

    def test_fully_withheld_item_requires_an_exemption_log_and_is_never_zipped(self):
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)",
            short_name="Personal information",
        )
        self.item.review_state = RecordItem.ReviewState.WITHHELD
        self.item.withholding_exemption = exemption
        self.item.save(update_fields=["review_state", "withholding_exemption"])
        client = FakeObjectClient({self.item.storage_key: self.payload})

        with self.assertRaisesMessage(ValidationError, "exemption log"):
            prepare_installment(
                self.installment.pk,
                actor=self.officer,
                _client=client,
            )

        self.assertEqual(client.get_calls, [])

    def test_prepare_rejects_a_stale_fully_withheld_exemption_log(self):
        original = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)",
            short_name="Personal information",
        )
        replacement = ExemptionCitation.objects.create(
            citation="RCW 42.56.250(1)",
            short_name="Employment information",
        )
        self.item.review_state = RecordItem.ReviewState.WITHHELD
        self.item.withholding_exemption = original
        self.item.save(update_fields=["review_state", "withholding_exemption"])
        client = FakeObjectClient({self.item.storage_key: self.payload})
        generate_exemption_log(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        self.item.withholding_exemption = replacement
        self.item.save(update_fields=["withholding_exemption"])

        with self.assertRaisesMessage(ValidationError, "changed after generation"):
            prepare_installment(
                self.installment.pk,
                actor=self.officer,
                _client=client,
            )

        self.installment.refresh_from_db()
        self.assertEqual(self.installment.package_storage_key, "")

    def test_tampered_record_aborts_without_package_or_audit(self):
        client = FakeObjectClient({self.item.storage_key: b"tampered"})

        with self.assertRaisesMessage(RuntimeError, "failed integrity"):
            prepare_installment(
                self.installment.pk,
                actor=self.officer,
                _client=client,
            )

        self.installment.refresh_from_db()
        self.assertEqual(self.installment.package_storage_key, "")
        self.assertEqual(client.put_calls, [])
        self.assertFalse(AuditLog.objects.filter(
            action="installment.prepared",
        ).exists())

    def test_non_officer_cannot_prepare_a_package(self):
        reviewer = get_user_model().objects.create_user(
            "reviewer",
            "reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})

        with self.assertRaises(PermissionDenied):
            prepare_installment(
                self.installment.pk,
                actor=reviewer,
                _client=FakeObjectClient({self.item.storage_key: self.payload}),
            )

    @override_settings(
        PORTAL_BASE_URL="https://prr.psd401.net",
        PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS=7,
    )
    def test_release_rotates_capability_and_sets_one_expiring_url(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        original_token = self.installment.download_token

        released, download_url = release_installment(
            self.installment.pk,
            actor=self.officer,
        )

        self.assertIsNotNone(released.released_at)
        self.assertNotEqual(released.download_token, original_token)
        self.assertEqual(
            released.download_expires_at,
            released.released_at + timedelta(days=7),
        )
        self.assertEqual(
            download_url,
            "https://prr.psd401.net/requests/"
            f"{self.prr.number}/installments/1/download/"
            f"{released.download_token}/",
        )
        event = AuditLog.objects.get(action="installment.released")
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.detail["package_sha256"], released.package_sha256)
        self.assertNotIn(str(released.download_token), str(event.detail))

        replay, replay_url = release_installment(
            self.installment.pk,
            actor=self.officer,
        )
        self.assertEqual(replay.download_token, released.download_token)
        self.assertEqual(replay_url, download_url)
        self.assertEqual(
            AuditLog.objects.filter(action="installment.released").count(),
            1,
        )

    @override_settings(
        PORTAL_BASE_URL="https://prr.psd401.net",
        PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS=7,
    )
    def test_expired_release_rotates_link_with_a_replacement_notice(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        first, first_url, first_notice = release_installment_with_notice(
            self.installment.pk,
            actor=self.officer,
            is_final=False,
        )
        first_token = first.download_token
        Installment.objects.filter(pk=first.pk).update(
            download_expires_at=timezone.now() - timedelta(seconds=1),
        )

        renewed, renewed_url, renewed_notice = release_installment_with_notice(
            self.installment.pk,
            actor=self.officer,
            is_final=False,
        )

        self.assertNotEqual(renewed.download_token, first_token)
        self.assertNotEqual(renewed_url, first_url)
        self.assertNotEqual(renewed_notice.pk, first_notice.pk)
        self.assertEqual(
            Correspondence.objects.filter(
                request=self.prr,
                kind=Correspondence.Kind.INSTALLMENT_NOTICE,
            ).count(),
            2,
        )
        renewal = AuditLog.objects.get(action="installment.download_renewed")
        self.assertEqual(renewal.actor, self.officer)
        self.assertNotIn(str(renewed.download_token), str(renewal.detail))

        with self.assertRaisesMessage(
            ValidationError,
            "release snapshot is stale",
        ):
            correspondence_services.approve_draft(
                first_notice,
                actor=self.officer,
                expected_version=first_notice.version,
            )

    @override_settings(
        PORTAL_BASE_URL="https://prr.psd401.net",
        PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS=7,
    )
    def test_a_later_production_deposit_does_not_strand_delivered_records(self):
        """Renewing a link re-delivers records already produced; it is not
        the new production work a deposit holds."""
        from datetime import date
        from decimal import Decimal

        from apps.records.fees import activate_fee_schedule, apply_deposit_gate
        from apps.records.models import FeeDepositGate, FeeSchedule

        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        first, _url, _notice = release_installment_with_notice(
            self.installment.pk,
            actor=self.officer,
            is_final=False,
        )
        activate_fee_schedule(
            actor=self.officer,
            name="PSD adopted PRA copy schedule",
            basis=FeeSchedule.Basis.STATUTORY_DEFAULT,
            effective_on=date(2026, 8, 1),
            counsel_approval_reference="Legal memo PRA-fees-2026-07-28",
            adoption_reference="Board policy 4040 resolution 2026-08-01",
        )
        apply_deposit_gate(
            self.prr.number,
            actor=self.officer,
            scope=FeeDepositGate.Scope.PRODUCTION,
            amount=Decimal("36.72"),
            estimated_total=Decimal("367.25"),
            reason="Ten percent deposit on the next installment's copying.",
        )
        Installment.objects.filter(pk=first.pk).update(
            download_expires_at=timezone.now() - timedelta(seconds=1),
        )

        renewed, _renewed_url, _renewed_notice = (
            release_installment_with_notice(
                self.installment.pk,
                actor=self.officer,
                is_final=False,
            )
        )

        self.assertNotEqual(renewed.download_token, first.download_token)

    @override_settings(
        PORTAL_BASE_URL="https://prr.psd401.net",
        PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS=7,
    )
    def test_expired_release_cannot_rotate_without_requester_notice_workflow(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        first, _url = release_installment(
            self.installment.pk,
            actor=self.officer,
        )
        first_token = first.download_token
        Installment.objects.filter(pk=first.pk).update(
            download_expires_at=timezone.now() - timedelta(seconds=1),
        )

        with self.assertRaisesMessage(
            ValidationError,
            "requester notice release workflow",
        ):
            release_installment(
                self.installment.pk,
                actor=self.officer,
            )

        self.installment.refresh_from_db()
        self.assertEqual(self.installment.download_token, first_token)
        self.assertFalse(AuditLog.objects.filter(
            action="installment.download_renewed",
        ).exists())

    def test_release_rejects_unprepared_or_unpaid_installment(self):
        with self.assertRaisesMessage(ValidationError, "prepared package"):
            release_installment(self.installment.pk, actor=self.officer)

        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        self.installment.fee_amount = "12.50"
        self.installment.save(update_fields=["fee_amount"])

        with self.assertRaisesMessage(ValidationError, "paid in full"):
            release_installment(self.installment.pk, actor=self.officer)

    @override_settings(
        PORTAL_BASE_URL="https://prr.psd401.net",
        PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS=7,
    )
    def test_release_atomically_creates_an_approvable_requester_notice(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )

        released, download_url, notice = release_installment_with_notice(
            self.installment.pk,
            actor=self.officer,
            is_final=True,
        )

        self.assertEqual(notice.kind, Correspondence.Kind.INSTALLMENT_NOTICE)
        self.assertEqual(notice.status, Correspondence.Status.DRAFT)
        self.assertEqual(notice.template_version, "3")
        self.assertIn("now available", notice.draft_body)
        self.assertIn(download_url, notice.draft_body)
        self.assertNotIn("cannot yet be sent", notice.draft_body)
        self.assertEqual(notice.draft_metadata["package_sha256"], released.package_sha256)
        self.assertEqual(notice.draft_metadata["is_final"], True)

        approved = correspondence_services.approve_draft(
            notice,
            actor=self.officer,
            expected_version=notice.version,
        )
        self.assertEqual(approved.status, Correspondence.Status.APPROVED)

    @override_settings(
        PORTAL_BASE_URL="https://prr.psd401.net",
        PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS=7,
    )
    def test_notice_approval_revalidates_package_review_snapshot(self):
        client = FakeObjectClient({self.item.storage_key: self.payload})
        prepare_installment(
            self.installment.pk,
            actor=self.officer,
            _client=client,
        )
        _released, _download_url, notice = release_installment_with_notice(
            self.installment.pk,
            actor=self.officer,
            is_final=True,
        )
        self.item.review_state = RecordItem.ReviewState.NON_RESPONSIVE
        self.item.save(update_fields=["review_state"])

        with self.assertRaisesMessage(ValidationError, "not releasable"):
            correspondence_services.approve_draft(
                notice,
                actor=self.officer,
                expected_version=notice.version,
            )

        notice.refresh_from_db()
        self.assertEqual(notice.status, Correspondence.Status.DRAFT)
