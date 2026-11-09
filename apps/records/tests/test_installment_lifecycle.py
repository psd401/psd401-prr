"""Creating, re-scoping, revoking, and code-gating one installment.

``test_installment_production`` covers turning a locked installment into
bytes. This module covers everything an officer does around that: choosing
what goes in, taking a link back, and putting a second factor on it.
"""

import hashlib
import io
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.correspondence.models import Correspondence, CorrespondenceDelivery
from apps.records.installments import (
    InstallmentNoticeUnsent,
    clear_installment_access_code,
    create_installment,
    release_installment_and_send_notice,
    release_ready_record_items,
    revoke_installment,
    set_installment_access_code,
    set_installment_items,
    unreleased_record_items,
)
from apps.records.models import (
    ExemptionCitation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    Redaction,
    Requester,
    RequestStatus,
)
from apps.records.production import (
    _artifact_for_item,
    prepare_installment,
    release_installment,
)

from .object_store_fakes import FakeObjectClient


class _InstallmentTestCase(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = get_user_model().objects.create_user(
            "reviewer", "reviewer@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        requester = Requester.objects.create(email="requester@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="Board records.",
        )

    def make_item(self, name="record.pdf", **overrides):
        payload = f"%PDF-1.7 {name}".encode()
        fields = {
            "request": self.prr,
            "original_filename": name,
            "storage_key": f"records/{self.prr.id}/{name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "content_length": len(payload),
            "content_type": "application/pdf",
            "review_state": RecordItem.ReviewState.RESPONSIVE,
            "reviewed_by": self.reviewer,
            "reviewed_at": timezone.now(),
        }
        fields.update(overrides)
        return RecordItem.objects.create(**fields)


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PORTAL_BASE_URL="https://prr.psd401.net",
    PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True,
    PRA_OFFICER_EMAIL="records@psd401.net",
)
class ReleasePathEndToEndTests(_InstallmentTestCase):
    """The whole path the Releases tab drives, in one pass.

    Select records, create an installment, package it, release and send in one
    action, let the requester download it, then revoke it. Every unit of this
    is tested elsewhere; this asserts they compose.
    """

    def test_create_release_send_download_then_revoke(self):
        payload = b"%PDF-1.7 responsive record"
        item = self.make_item("board-minutes.pdf")
        RecordItem.objects.filter(pk=item.pk).update(
            sha256=hashlib.sha256(payload).hexdigest(),
            content_length=len(payload),
        )
        item.refresh_from_db()

        # 1. The ready list is what an officer selects from.
        ready = release_ready_record_items(
            unreleased_record_items(RecordItem.objects.filter(request=self.prr))
        )
        self.assertEqual(list(ready), [item])

        # 2. Create from the selection; 3. package it.
        installment = create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[item.pk],
        )
        client = FakeObjectClient({item.storage_key: payload})
        prepare_installment(installment.pk, actor=self.officer, _client=client)

        # 4. One action releases, drafts the notice, approves it, and hands
        #    it to delivery -- under one named officer, in one click.
        released, download_url, notice = release_installment_and_send_notice(
            installment.pk,
            actor=self.officer,
            is_final=True,
        )
        self.assertEqual(notice.status, Correspondence.Status.QUEUED)
        self.assertEqual(notice.approved_by, self.officer)
        self.assertTrue(
            CorrespondenceDelivery.objects.filter(
                correspondence=notice,
            ).exists()
        )

        self.assertTrue(
            download_url.startswith("https://prr.psd401.net/requests/")
        )
        self.assertEqual(notice.approved_metadata["download_url"], download_url)
        self.assertIn(download_url, notice.approved_body)
        self.assertTrue(
            AuditLog.objects.filter(
                action="installment.released_and_sent",
                target=self.prr.number,
            ).exists()
        )

        # 5. The requester reads the page, then downloads the package.
        path = download_url.replace("https://prr.psd401.net", "")
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "records@psd401.net")

        with patch(
            "apps.records.requester_downloads.load_verified_object"
        ) as verify:
            verify.side_effect = lambda *a, **k: io.BytesIO(
                client.objects[released.package_storage_key]
            )
            download = self.client.get(path + "package/")
            self.assertEqual(download.status_code, 200)
            b"".join(download.streaming_content)

        released.refresh_from_db()
        self.assertEqual(released.download_count, 1)

        # 6. Revoking takes the link back; the artifact and audit trail stay.
        revoke_installment(
            installment.pk,
            actor=self.officer,
            reason="The wrong custodian mailbox was packaged.",
        )

        self.assertEqual(self.client.get(path).status_code, 410)
        self.assertEqual(self.client.get(path + "package/").status_code, 410)
        released.refresh_from_db()
        self.assertTrue(released.package_storage_key)
        self.assertEqual(released.download_count, 1)
        # And the records are free to go into a replacement installment.
        self.assertEqual(
            list(unreleased_record_items(RecordItem.objects.all())),
            [item],
        )


class ReleaseReadyFilterTests(_InstallmentTestCase):
    def test_unreviewed_and_non_responsive_records_are_not_ready(self):
        ready = self.make_item("ready.pdf")
        self.make_item(
            "unreviewed.pdf",
            review_state=RecordItem.ReviewState.UNREVIEWED,
            reviewed_by=None,
            reviewed_at=None,
        )
        self.make_item(
            "non-responsive.pdf",
            review_state=RecordItem.ReviewState.NON_RESPONSIVE,
        )
        self.make_item(
            "duplicate.pdf",
            review_state=RecordItem.ReviewState.DUPLICATE,
        )

        self.assertEqual(
            list(release_ready_record_items(RecordItem.objects.all())),
            [ready],
        )

    def test_withheld_record_with_its_citation_is_ready(self):
        citation = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)",
            short_name="Personal information",
        )
        withheld = self.make_item(
            "withheld.pdf",
            review_state=RecordItem.ReviewState.WITHHELD,
            withholding_exemption=citation,
        )

        self.assertIn(
            withheld,
            release_ready_record_items(RecordItem.objects.all()),
        )

    def test_outstanding_second_review_and_missing_redacted_bytes_block(self):
        heightened = ExemptionCitation.objects.create(
            citation="RCW 42.56.240(1)",
            short_name="Investigative",
            requires_second_review=True,
        )
        ordinary = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(3)",
            short_name="Personnel",
        )
        awaiting = self.make_item("awaiting.pdf")
        Redaction.objects.create(
            record_item=awaiting,
            page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=heightened,
            created_by=self.reviewer,
        )
        unrendered = self.make_item("unrendered.pdf")
        Redaction.objects.create(
            record_item=unrendered,
            page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=ordinary,
            created_by=self.reviewer,
        )

        ready = set(
            release_ready_record_items(
                RecordItem.objects.all()
            ).values_list("pk", flat=True)
        )

        self.assertNotIn(awaiting.pk, ready)
        self.assertNotIn(unrendered.pk, ready)

    def test_rendered_redacted_record_becomes_ready(self):
        ordinary = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(3)",
            short_name="Personnel",
        )
        item = self.make_item(
            "rendered.pdf",
            redacted_storage_key="redacted/one.pdf",
            redacted_sha256="a" * 64,
            redacted_content_length=12,
            redacted_content_type="application/pdf",
            redacted_manifest_sha256="b" * 64,
        )
        Redaction.objects.create(
            record_item=item,
            page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=ordinary,
            created_by=self.reviewer,
        )

        self.assertIn(
            item,
            release_ready_record_items(RecordItem.objects.all()),
        )

    def test_release_ready_filter_matches_production(self):
        """The SQL filter and the packaging check must agree, item by item."""
        heightened = ExemptionCitation.objects.create(
            citation="RCW 42.56.240(1)",
            short_name="Investigative",
            requires_second_review=True,
        )
        ordinary = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(3)",
            short_name="Personnel",
        )
        candidates = [
            self.make_item("plain.pdf"),
            self.make_item(
                "unreviewed.pdf",
                review_state=RecordItem.ReviewState.UNREVIEWED,
                reviewed_by=None,
                reviewed_at=None,
            ),
            self.make_item(
                "non-responsive.pdf",
                review_state=RecordItem.ReviewState.NON_RESPONSIVE,
            ),
            self.make_item(
                "withheld.pdf",
                review_state=RecordItem.ReviewState.WITHHELD,
                withholding_exemption=ordinary,
            ),
            self.make_item(
                "withheld-heightened.pdf",
                review_state=RecordItem.ReviewState.WITHHELD,
                withholding_exemption=heightened,
            ),
            self.make_item(
                "rendered.pdf",
                redacted_storage_key="redacted/two.pdf",
                redacted_sha256="c" * 64,
                redacted_content_length=12,
                redacted_content_type="application/pdf",
                redacted_manifest_sha256="d" * 64,
            ),
            self.make_item("unrendered.pdf"),
        ]
        Redaction.objects.create(
            record_item=candidates[5],
            page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=ordinary,
            created_by=self.reviewer,
        )
        Redaction.objects.create(
            record_item=candidates[6],
            page_number=1,
            x0=0.1, y0=0.1, x1=0.2, y1=0.2,
            exemption=ordinary,
            created_by=self.reviewer,
        )

        ready = set(
            release_ready_record_items(
                RecordItem.objects.all()
            ).values_list("pk", flat=True)
        )

        for item in candidates:
            item.refresh_from_db()
            try:
                _artifact_for_item(item, 1)
            except ValidationError:
                producible = False
            else:
                producible = True
            with self.subTest(item=item.original_filename):
                self.assertEqual(item.pk in ready, producible)


class InstallmentMembershipTests(_InstallmentTestCase):
    def test_create_assigns_the_next_sequence_and_audits_membership(self):
        first = self.make_item("one.pdf")
        second = self.make_item("two.pdf")

        installment = create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[first.pk, second.pk],
        )

        self.assertEqual(installment.sequence, 1)
        self.assertCountEqual(list(installment.items.all()), [first, second])
        event = AuditLog.objects.get(action="installment.created")
        self.assertEqual(event.actor, self.officer)
        self.assertEqual(event.target, self.prr.number)
        self.assertEqual(event.detail["item_count"], 2)

        third = self.make_item("three.pdf")
        again = create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[third.pk],
        )
        self.assertEqual(again.sequence, 2)

    def test_more_than_one_page_of_records_all_reach_the_installment(self):
        # #30: the Releases list pages at 50; an installment is not.
        items = [self.make_item(f"record-{index}.pdf") for index in range(60)]

        installment = create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[item.pk for item in items],
        )

        self.assertEqual(installment.items.count(), 60)

    def test_only_a_current_records_officer_may_create(self):
        item = self.make_item()
        with self.assertRaises(PermissionDenied):
            create_installment(
                self.prr.number,
                actor=self.reviewer,
                item_ids=[item.pk],
            )

    def test_records_from_another_request_are_refused(self):
        other_requester = Requester.objects.create(email="other@example.org")
        other = PublicRecordsRequest.objects.create(
            requester=other_requester,
            description="Other.",
        )
        foreign = RecordItem.objects.create(
            request=other,
            storage_key="records/foreign.pdf",
            sha256="e" * 64,
            review_state=RecordItem.ReviewState.RESPONSIVE,
            reviewed_by=self.reviewer,
            reviewed_at=timezone.now(),
        )
        with self.assertRaisesMessage(
            ValidationError,
            "must belong to this request",
        ):
            create_installment(
                self.prr.number,
                actor=self.officer,
                item_ids=[foreign.pk],
            )

    def test_records_that_are_not_ready_are_refused(self):
        unreviewed = self.make_item(
            "unreviewed.pdf",
            review_state=RecordItem.ReviewState.UNREVIEWED,
            reviewed_by=None,
            reviewed_at=None,
        )
        with self.assertRaisesMessage(
            ValidationError,
            "not ready for release",
        ):
            create_installment(
                self.prr.number,
                actor=self.officer,
                item_ids=[unreviewed.pk],
            )

    def test_a_record_cannot_be_in_two_live_installments(self):
        item = self.make_item()
        create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[item.pk],
        )
        with self.assertRaisesMessage(
            ValidationError,
            "already in a live installment",
        ):
            create_installment(
                self.prr.number,
                actor=self.officer,
                item_ids=[item.pk],
            )

    def test_empty_selection_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "at least one"):
            create_installment(
                self.prr.number,
                actor=self.officer,
                item_ids=[],
            )

    @override_settings(PRR_INSTALLMENT_MAX_ITEMS=1)
    def test_selection_is_capped(self):
        items = [self.make_item("a.pdf"), self.make_item("b.pdf")]
        with self.assertRaisesMessage(ValidationError, "at most 1"):
            create_installment(
                self.prr.number,
                actor=self.officer,
                item_ids=[item.pk for item in items],
            )

    def test_set_items_replaces_membership_while_open(self):
        first = self.make_item("one.pdf")
        second = self.make_item("two.pdf")
        installment = create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[first.pk],
        )

        set_installment_items(
            installment.pk,
            actor=self.officer,
            item_ids=[second.pk],
        )

        self.assertCountEqual(list(installment.items.all()), [second])
        event = AuditLog.objects.get(action="installment.items_set")
        self.assertEqual(event.detail["previous_record_item_ids"], [str(first.pk)])
        self.assertEqual(event.detail["record_item_ids"], [str(second.pk)])

    def test_set_items_is_refused_once_a_package_is_prepared(self):
        item = self.make_item()
        other = self.make_item("two.pdf")
        installment = create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[item.pk],
        )
        Installment.objects.filter(pk=installment.pk).update(
            package_storage_key="productions/one.zip",
            package_sha256="f" * 64,
            package_size=10,
            package_manifest_sha256="0" * 64,
            package_prepared_at=timezone.now(),
        )

        with self.assertRaisesMessage(ValidationError, "locked once a package"):
            set_installment_items(
                installment.pk,
                actor=self.officer,
                item_ids=[other.pk],
            )

    def test_unreleased_filter_excludes_live_membership_but_not_revoked(self):
        claimed = self.make_item("claimed.pdf")
        free = self.make_item("free.pdf")
        installment = create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[claimed.pk],
        )

        self.assertEqual(
            list(unreleased_record_items(RecordItem.objects.all())),
            [free],
        )

        Installment.objects.filter(pk=installment.pk).update(
            released_at=timezone.now(),
            revoked_at=timezone.now(),
            revocation_reason="Packaged the wrong custodian's mail.",
        )

        self.assertCountEqual(
            list(unreleased_record_items(RecordItem.objects.all())),
            [claimed, free],
        )


class RevocationTests(_InstallmentTestCase):
    def setUp(self):
        super().setUp()
        self.package = b"PK\x03\x04verified zip"
        self.installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            package_storage_key=f"productions/{self.prr.id}/1/generated.zip",
            package_sha256=hashlib.sha256(self.package).hexdigest(),
            package_size=len(self.package),
            package_manifest_sha256="a" * 64,
            package_prepared_at=timezone.now(),
            released_at=timezone.now(),
            download_expires_at=timezone.now() + timedelta(hours=1),
        )

    def url(self):
        return (
            f"/requests/{self.prr.number}/installments/1/download/"
            f"{self.installment.download_token}/"
        )

    def test_revocation_requires_a_substantive_reason(self):
        with self.assertRaisesMessage(ValidationError, "substantive reason"):
            revoke_installment(
                self.installment.pk,
                actor=self.officer,
                reason="oops",
            )

    def test_revocation_is_recorded_and_cannot_repeat(self):
        revoke_installment(
            self.installment.pk,
            actor=self.officer,
            reason="Packaged the wrong custodian's mail.",
        )

        self.installment.refresh_from_db()
        self.assertIsNotNone(self.installment.revoked_at)
        self.assertEqual(self.installment.revoked_by, self.officer)
        event = AuditLog.objects.get(action="installment.revoked")
        self.assertEqual(event.actor, self.officer)
        self.assertIn("wrong custodian", event.detail["reason"])

        with self.assertRaisesMessage(ValidationError, "already revoked"):
            revoke_installment(
                self.installment.pk,
                actor=self.officer,
                reason="Packaged the wrong custodian's mail.",
            )

    def test_an_unreleased_installment_cannot_be_revoked(self):
        Installment.objects.filter(pk=self.installment.pk).update(
            released_at=None,
        )
        with self.assertRaisesMessage(ValidationError, "Only a released"):
            revoke_installment(
                self.installment.pk,
                actor=self.officer,
                reason="Packaged the wrong custodian's mail.",
            )

    def test_a_revoked_installment_cannot_be_released_again(self):
        revoke_installment(
            self.installment.pk,
            actor=self.officer,
            reason="Packaged the wrong custodian's mail.",
        )

        with self.assertRaisesMessage(ValidationError, "revoked"):
            release_installment(self.installment.pk, actor=self.officer)

        self.assertFalse(
            AuditLog.objects.filter(
                action__in=[
                    "installment.released",
                    "installment.download_renewed",
                ],
            ).exists()
        )

    def test_a_closed_request_cannot_release(self):
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.FULFILLED,
        )

        with self.assertRaisesMessage(ValidationError, "closed"):
            release_installment(self.installment.pk, actor=self.officer)

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_a_revoked_link_answers_gone_on_page_and_package(self, verify):
        revoke_installment(
            self.installment.pk,
            actor=self.officer,
            reason="Packaged the wrong custodian's mail.",
        )

        page = self.client.get(self.url())
        package = self.client.get(self.url() + "package/")

        self.assertEqual(page.status_code, 410)
        self.assertContains(page, "withdrawn", status_code=410)
        self.assertEqual(package.status_code, 410)
        verify.assert_not_called()
        self.assertTrue(
            AuditLog.objects.filter(
                action="installment.download_revoked",
                target=self.prr.number,
            ).exists()
        )


class RequesterDownloadPageTests(_InstallmentTestCase):
    def setUp(self):
        super().setUp()
        self.package = b"PK\x03\x04verified zip"
        self.item = self.make_item("one.pdf")
        self.installment = Installment.objects.create(
            request=self.prr,
            sequence=1,
            package_storage_key=f"productions/{self.prr.id}/1/generated.zip",
            package_sha256=hashlib.sha256(self.package).hexdigest(),
            package_size=len(self.package),
            package_manifest_sha256="a" * 64,
            package_prepared_at=timezone.now(),
            released_at=timezone.now(),
            download_expires_at=timezone.now() + timedelta(days=7),
        )
        self.installment.items.add(self.item)

    def url(self):
        return (
            f"/requests/{self.prr.number}/installments/1/download/"
            f"{self.installment.download_token}/"
        )

    @override_settings(PRA_OFFICER_EMAIL="records@psd401.net")
    def test_page_states_contents_expiry_and_contact_before_the_package(self):
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        self.assertContains(response, self.prr.number)
        self.assertContains(response, "records@psd401.net")
        self.assertContains(response, "Records included")
        self.assertContains(response, self.url() + "package/")
        self.assertNotContains(response, "requester@example.org")

    def test_an_unknown_token_gives_the_same_page_as_a_missing_one(self):
        response = self.client.get(
            f"/requests/{self.prr.number}/installments/1/download/"
            "00000000-0000-4000-8000-000000000000/"
        )

        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "not valid", status_code=404)

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_the_download_counter_advances_with_each_delivery(self, verify):
        import io as _io

        verify.side_effect = lambda *a, **k: _io.BytesIO(self.package)

        for expected in (1, 2):
            response = self.client.get(self.url() + "package/")
            self.assertEqual(response.status_code, 200)
            b"".join(response.streaming_content)
            self.installment.refresh_from_db()
            self.assertEqual(self.installment.download_count, expected)
            self.assertIsNotNone(self.installment.last_downloaded_at)
        self.assertEqual(
            AuditLog.objects.filter(action="installment.downloaded").count(),
            2,
        )

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_a_failed_download_does_not_advance_the_counter(self, verify):
        verify.side_effect = RuntimeError("storage is down")

        response = self.client.get(self.url() + "package/")

        self.assertEqual(response.status_code, 503)
        self.installment.refresh_from_db()
        self.assertEqual(self.installment.download_count, 0)


class AccessCodeTests(RequesterDownloadPageTests):
    def test_setting_a_code_stores_only_a_hash(self):
        set_installment_access_code(
            self.installment.pk,
            actor=self.officer,
            access_code="salmon-bay-42",
        )

        self.installment.refresh_from_db()
        self.assertTrue(self.installment.access_code_hash)
        self.assertNotIn("salmon-bay-42", self.installment.access_code_hash)
        self.assertEqual(self.installment.access_code_set_by, self.officer)
        event = AuditLog.objects.get(action="installment.access_code_set")
        self.assertNotIn("salmon-bay-42", str(event.detail))

    def test_a_short_code_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "at least"):
            set_installment_access_code(
                self.installment.pk,
                actor=self.officer,
                access_code="short",
            )

    def test_only_a_records_officer_may_set_or_clear_a_code(self):
        with self.assertRaises(PermissionDenied):
            set_installment_access_code(
                self.installment.pk,
                actor=self.reviewer,
                access_code="salmon-bay-42",
            )

    def test_a_revoked_installment_keeps_whatever_code_it_had(self):
        set_installment_access_code(
            self.installment.pk,
            actor=self.officer,
            access_code="salmon-bay-42",
        )
        revoke_installment(
            self.installment.pk,
            actor=self.officer,
            reason="Packaged the wrong custodian's mail.",
        )

        for change in (
            lambda: clear_installment_access_code(
                self.installment.pk, actor=self.officer,
            ),
            lambda: set_installment_access_code(
                self.installment.pk,
                actor=self.officer,
                access_code="another-code-99",
            ),
        ):
            with self.assertRaisesMessage(ValidationError, "revoked"):
                change()
        self.installment.refresh_from_db()
        self.assertTrue(self.installment.access_code_hash)

    def test_clearing_requires_a_code_to_be_set(self):
        with self.assertRaisesMessage(ValidationError, "no access code"):
            clear_installment_access_code(
                self.installment.pk,
                actor=self.officer,
            )
        set_installment_access_code(
            self.installment.pk,
            actor=self.officer,
            access_code="salmon-bay-42",
        )
        clear_installment_access_code(self.installment.pk, actor=self.officer)
        self.installment.refresh_from_db()
        self.assertEqual(self.installment.access_code_hash, "")
        self.assertIsNone(self.installment.access_code_set_at)

    def test_a_closed_request_can_still_manage_a_live_link(self):
        """A released link outlives closure; managing it needs no reopening."""
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.FULFILLED,
        )

        set_installment_access_code(
            self.installment.pk,
            actor=self.officer,
            access_code="salmon-bay-42",
        )
        clear_installment_access_code(self.installment.pk, actor=self.officer)
        revoke_installment(
            self.installment.pk,
            actor=self.officer,
            reason="The requester reports the link was forwarded.",
        )

        self.installment.refresh_from_db()
        self.assertIsNotNone(self.installment.revoked_at)

    def test_a_repeated_or_sequential_code_is_refused(self):
        for weak in ("aaaaaaaa", "12345678", "abcdefghij", "98765432"):
            with self.subTest(code=weak):
                with self.assertRaisesMessage(
                    ValidationError, "repeated or sequential"
                ):
                    set_installment_access_code(
                        self.installment.pk,
                        actor=self.officer,
                        access_code=weak,
                    )
        self.installment.refresh_from_db()
        self.assertEqual(self.installment.access_code_hash, "")

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_surrounding_whitespace_is_not_part_of_the_code(self, verify):
        verify.side_effect = lambda *a, **k: io.BytesIO(self.package)
        set_installment_access_code(
            self.installment.pk,
            actor=self.officer,
            access_code="  salmon-bay-42  ",
        )

        right = self.client.post(
            self.url() + "package/",
            {"access_code": "salmon-bay-42"},
        )

        self.assertEqual(right.status_code, 200)
        b"".join(right.streaming_content)

        # And the other way round: a code pasted from an email with a
        # trailing newline or space is still the code, not a guess.
        pasted = self.client.post(
            self.url() + "package/",
            {"access_code": " salmon-bay-42\n"},
        )
        self.assertEqual(pasted.status_code, 200)
        b"".join(pasted.streaming_content)
        self.assertFalse(
            AuditLog.objects.filter(
                action="installment.download_code_rejected",
            ).exists()
        )

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_a_code_is_only_read_from_a_post(self, verify):
        """A GET with a body must not be a throttle-free way to guess.

        DRF parses a request body by content type, not by method, so a GET
        carrying ``access_code`` would otherwise be checked like a POST while
        both guess throttles, which count only POSTs, stayed silent.
        """
        verify.side_effect = lambda *a, **k: io.BytesIO(self.package)
        set_installment_access_code(
            self.installment.pk,
            actor=self.officer,
            access_code="salmon-bay-42",
        )

        for content_type, body in (
            ("application/json", '{"access_code": "salmon-bay-42"}'),
            ("application/x-www-form-urlencoded", "access_code=salmon-bay-42"),
        ):
            with self.subTest(content_type=content_type):
                response = self.client.generic(
                    "GET",
                    self.url() + "package/",
                    data=body,
                    content_type=content_type,
                )
                self.assertEqual(response.status_code, 403)
        verify.assert_not_called()
        # A GET is a bookmark or a direct visit, not an attempt: the form is
        # shown and nothing is recorded as rejected.
        self.assertFalse(
            AuditLog.objects.filter(
                action="installment.download_code_rejected",
            ).exists()
        )
        self.assertNotContains(response, "not correct", status_code=403)

    @override_settings(
        PRR_INSTALLMENT_ACCESS_CODE_PER_IP_RATE="100/hour",
        PRR_INSTALLMENT_ACCESS_CODE_PER_INSTALLMENT_RATE="2/hour",
    )
    @patch("apps.records.requester_downloads.load_verified_object")
    def test_guessing_is_capped_per_installment_across_addresses(self, verify):
        """Spreading guesses over many addresses buys nothing.

        The per-address ceiling is the first line; this is the second, so a
        leaked link cannot be brute-forced from a pool of source addresses.
        """
        cache.clear()
        verify.side_effect = lambda *a, **k: io.BytesIO(self.package)
        set_installment_access_code(
            self.installment.pk,
            actor=self.officer,
            access_code="salmon-bay-42",
        )

        statuses = [
            self.client.post(
                self.url() + "package/",
                {"access_code": "not-the-code"},
                REMOTE_ADDR=f"203.0.113.{octet}",
            ).status_code
            for octet in (1, 2, 3)
        ]
        self.assertEqual(statuses, [403, 403, 429])

        # Once the ceiling is hit, even the right code waits for the window.
        right = self.client.post(
            self.url() + "package/",
            {"access_code": "salmon-bay-42"},
            REMOTE_ADDR="203.0.113.4",
        )
        self.assertEqual(right.status_code, 429)
        verify.assert_not_called()

    @patch("apps.records.requester_downloads.load_verified_object")
    def test_the_page_asks_for_the_code_and_the_package_requires_it(self, verify):
        import io as _io

        verify.side_effect = lambda *a, **k: _io.BytesIO(self.package)
        set_installment_access_code(
            self.installment.pk,
            actor=self.officer,
            access_code="salmon-bay-42",
        )

        page = self.client.get(self.url())
        self.assertContains(page, "Access code")
        self.assertNotContains(page, "salmon-bay-42")

        bare = self.client.get(self.url() + "package/")
        self.assertEqual(bare.status_code, 403)
        verify.assert_not_called()

        wrong = self.client.post(
            self.url() + "package/",
            {"access_code": "not-the-code"},
        )
        self.assertEqual(wrong.status_code, 403)
        self.assertContains(wrong, "not correct", status_code=403)
        # The rendered page goes through the same finalize path as JSON, so
        # a wrong-code answer is never cacheable or referrer-leaking either.
        self.assertEqual(wrong["Cache-Control"], "private, no-store")
        self.assertEqual(wrong["Referrer-Policy"], "no-referrer")
        verify.assert_not_called()
        self.assertTrue(
            AuditLog.objects.filter(
                action="installment.download_code_rejected",
            ).exists()
        )

        right = self.client.post(
            self.url() + "package/",
            {"access_code": "salmon-bay-42"},
        )
        self.assertEqual(right.status_code, 200)
        self.assertEqual(b"".join(right.streaming_content), self.package)


@override_settings(
    OBJECT_STORE={"bucket": "records"},
    PORTAL_BASE_URL="https://prr.psd401.net",
    PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True,
    PRA_OFFICER_EMAIL="records@psd401.net",
)
class ReleaseAndSendEdgeTests(_InstallmentTestCase):
    """What one-action release does when the send half cannot happen."""

    def prepared_installment(self):
        payload = b"%PDF-1.7 responsive record"
        item = self.make_item("board-minutes.pdf")
        RecordItem.objects.filter(pk=item.pk).update(
            sha256=hashlib.sha256(payload).hexdigest(),
            content_length=len(payload),
        )
        installment = create_installment(
            self.prr.number,
            actor=self.officer,
            item_ids=[item.pk],
        )
        client = FakeObjectClient({item.storage_key: payload})
        prepare_installment(installment.pk, actor=self.officer, _client=client)
        return installment

    def test_a_repeat_does_not_record_a_send_that_did_not_happen(self):
        installment = self.prepared_installment()
        release_installment_and_send_notice(
            installment.pk, actor=self.officer, is_final=False,
        )

        _, _, notice = release_installment_and_send_notice(
            installment.pk, actor=self.officer, is_final=False,
        )

        self.assertEqual(notice.status, Correspondence.Status.QUEUED)
        self.assertEqual(
            CorrespondenceDelivery.objects.filter(correspondence=notice).count(),
            1,
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="installment.released_and_sent",
            ).count(),
            1,
        )

    def test_a_release_whose_notice_cannot_send_stands_and_says_so(self):
        installment = self.prepared_installment()

        with self.settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=False):
            with self.assertRaisesMessage(
                InstallmentNoticeUnsent, "released"
            ) as caught:
                release_installment_and_send_notice(
                    installment.pk, actor=self.officer, is_final=False,
                )

        # The release is real and the officer is told so, not shown a bare
        # transport error that reads as "nothing happened".
        self.assertIn("transport is disabled", str(caught.exception))
        installment.refresh_from_db()
        self.assertIsNotNone(installment.released_at)
        notice = Correspondence.objects.get(
            request=self.prr, kind=Correspondence.Kind.INSTALLMENT_NOTICE,
        )
        self.assertEqual(notice.status, Correspondence.Status.DRAFT)
        unsent = AuditLog.objects.get(action="installment.release_notice_unsent")
        self.assertEqual(unsent.actor, self.officer)
        self.assertIn("transport is disabled", unsent.detail["reason"])
        self.assertFalse(
            AuditLog.objects.filter(
                action="installment.released_and_sent",
            ).exists()
        )

        # Pressing the button again once the cause is fixed finishes the job.
        _, _, sent = release_installment_and_send_notice(
            installment.pk, actor=self.officer, is_final=False,
        )
        self.assertEqual(sent.pk, notice.pk)
        self.assertEqual(sent.status, Correspondence.Status.QUEUED)
        self.assertEqual(
            AuditLog.objects.filter(
                action="installment.released_and_sent",
            ).count(),
            1,
        )

    def test_a_closed_request_cannot_change_installment_contents(self):
        item = self.make_item("one.pdf")
        other = self.make_item("two.pdf")
        installment = create_installment(
            self.prr.number, actor=self.officer, item_ids=[item.pk],
        )
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.CLOSED_WITHDRAWN,
        )

        with self.assertRaisesMessage(ValidationError, "closed"):
            set_installment_items(
                installment.pk, actor=self.officer, item_ids=[other.pk],
            )
        self.assertEqual(list(installment.items.all()), [item])

    def test_a_closed_request_cannot_open_an_installment(self):
        item = self.make_item("late.pdf")
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(
            status=RequestStatus.CLOSED_WITHDRAWN,
        )

        with self.assertRaisesMessage(ValidationError, "closed"):
            create_installment(
                self.prr.number, actor=self.officer, item_ids=[item.pk],
            )
        self.assertFalse(Installment.objects.exists())
