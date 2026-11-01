"""Requester possession links are scoped, expiring, and integrity checked."""

import hashlib
import io
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.records.models import Installment, PublicRecordsRequest, Requester
from apps.records.storage import ObjectIntegrityError


class InstallmentDownloadTests(TestCase):
    def setUp(self):
        requester = Requester.objects.create(email="requester@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Released records.",
        )
        self.package = b"PK\x03\x04verified zip"
        self.installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            package_storage_key=(
                f"productions/{self.prr.id}/1/generated.zip"
            ),
            package_sha256=hashlib.sha256(self.package).hexdigest(),
            package_size=len(self.package),
            package_manifest_sha256="a" * 64,
            package_prepared_at=timezone.now(),
            released_at=timezone.now(),
            download_expires_at=timezone.now() + timedelta(hours=1),
        )

    def url(self, token=None):
        """The page a requester lands on."""
        return (
            f"/requests/{self.prr.number}/installments/1/download/"
            f"{token or self.installment.download_token}/"
        )

    def package_url(self, token=None):
        """The bytes, one path deeper, behind any access code."""
        return self.url(token) + "package/"

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_valid_link_streams_only_the_verified_package(self, verify):
        verify.return_value = io.BytesIO(self.package)

        response = self.client.get(
            self.package_url(), REMOTE_ADDR="203.0.113.8"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.package)
        verify.assert_called_once_with(
            self.installment.package_storage_key,
            expected_size=len(self.package),
            expected_sha256=self.installment.package_sha256,
        )
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["Content-Length"], str(len(self.package)))
        self.assertIn(
            f'{self.prr.number}-installment-1.zip',
            response["Content-Disposition"],
        )
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        event = AuditLog.objects.get(action="installment.downloaded")
        self.assertEqual(event.target, self.prr.number)
        self.assertEqual(event.ip_address, "203.0.113.8")
        self.assertEqual(event.detail["installment_id"], self.installment.pk)
        self.assertNotIn("download_token", event.detail)
        self.assertNotIn(str(self.installment.download_token), str(event.detail))

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_wrong_or_malformed_token_is_indistinguishable_from_missing(self, verify):
        for token in (uuid.uuid4(), "not-a-token"):
            with self.subTest(token=token):
                response = self.client.get(self.package_url(token))
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response["Cache-Control"], "private, no-store")
        verify.assert_not_called()
        self.assertFalse(AuditLog.objects.exists())

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_expired_link_returns_gone_without_opening_storage(self, verify):
        self.installment.download_expires_at = timezone.now() - timedelta(seconds=1)
        self.installment.save(update_fields=["download_expires_at"])

        response = self.client.get(self.package_url())

        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json(), {"detail": "This download link has expired."})
        self.assertEqual(response["Cache-Control"], "private, no-store")
        verify.assert_not_called()
        self.assertTrue(AuditLog.objects.filter(
            action="installment.download_expired",
            target=self.prr.number,
        ).exists())

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_unreleased_unprepared_or_unpaid_package_is_not_disclosed(self, verify):
        prepared = {
            "package_storage_key": self.installment.package_storage_key,
            "package_sha256": self.installment.package_sha256,
            "package_size": self.installment.package_size,
            "package_manifest_sha256": self.installment.package_manifest_sha256,
            "package_prepared_at": self.installment.package_prepared_at,
        }
        scenarios = [
            {"released_at": None},
            {
                "package_storage_key": "",
                "package_sha256": "",
                "package_size": None,
                "package_manifest_sha256": "",
                "package_prepared_at": None,
            },
            {"fee_amount": "5.00", "fee_paid_at": None},
        ]
        for changes in scenarios:
            with self.subTest(changes=changes):
                Installment.objects.filter(pk=self.installment.pk).update(**changes)
                response = self.client.get(self.package_url())
                self.assertEqual(response.status_code, 404)
                self.installment.refresh_from_db()
                self.installment.released_at = timezone.now()
                for field, value in prepared.items():
                    setattr(self.installment, field, value)
                self.installment.fee_amount = 0
                self.installment.fee_paid_at = None
                self.installment.save(update_fields=[
                    "released_at",
                    *prepared,
                    "fee_amount",
                    "fee_paid_at",
                ])
        verify.assert_not_called()

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_integrity_failure_blocks_delivery_and_is_audited(self, verify):
        verify.side_effect = ObjectIntegrityError("tampered")

        response = self.client.get(self.package_url())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"detail": "The download is temporarily unavailable."},
        )
        self.assertTrue(AuditLog.objects.filter(
            action="installment.download_blocked",
            target=self.prr.number,
            detail__reason="integrity_check_failed",
        ).exists())
        self.assertFalse(AuditLog.objects.filter(
            action="installment.downloaded",
        ).exists())
