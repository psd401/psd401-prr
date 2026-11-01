"""REST surface for review throughput: filters, facets, bulk review, counsel."""

import hashlib

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records.models import (
    AdvisoryRun,
    CounselDomain,
    PublicRecordsRequest,
    RecordItem,
    RecordItemCustodian,
    Requester,
)


@override_settings(PRR_DISTRICT_EMAIL_DOMAINS=("psd401.net",))
class RecordReviewFilterApiTests(TestCase):
    def setUp(self):
        self.reviewer = get_user_model().objects.create_user(
            "throughput-reviewer",
            "throughput-reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])
        self.client.login(username=self.reviewer.username, password="pw")
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Filter the review queue.",
        )
        self.index = 0
        self.auto_reply = self._item(
            original_filename="Out of Office: budget.pdf",
            email_auto_submitted="auto-replied",
            email_sender_domain="psd401.net",
            custodian="alice@psd401.net",
        )
        self.external = self._item(
            original_filename="Vendor invoice.pdf",
            email_sender_domain="vendor.example",
            email_from="ann@vendor.example",
            custodian="bob@psd401.net",
            ocr_text="Invoice for playground resurfacing.",
        )
        self.privileged = self._item(
            original_filename="Memo.pdf",
            email_sender_domain="lawfirm.example",
            email_from="counsel@lawfirm.example",
            custodian="alice@psd401.net",
            ocr_text="Attorney-client communication about the claim.",
        )

    def _item(self, **fields):
        self.index += 1
        defaults = {
            "request": self.prr,
            "storage_key": f"records/{self.prr.pk}/{self.index}.pdf",
            "content_length": 100 + self.index,
            "content_type": "application/pdf",
            "sha256": hashlib.sha256(str(self.index).encode()).hexdigest(),
        }
        defaults.update(fields)
        item = RecordItem.objects.create(**defaults)
        if item.custodian:
            RecordItemCustodian.objects.create(
                record_item=item, custodian=item.custodian,
            )
        return item

    def _ids(self, response):
        return {row["id"] for row in response.json()["results"]}

    def test_the_record_row_carries_its_metadata_tags_and_custodian_set(self):
        response = self.client.get(
            f"/api/staff/record-items/{self.external.pk}/"
        )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["email_sender_domain"], "vendor.example")
        self.assertEqual(body["custodians"], ["bob@psd401.net"])
        self.assertIn("external_sender", body["tags"])
        self.assertNotIn("ocr_text", body)

    def test_filtering_by_tag_returns_only_that_tag(self):
        response = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "tag": "auto_reply"},
        )

        self.assertEqual(self._ids(response), {str(self.auto_reply.pk)})

    def test_the_privilege_queue_holds_counsel_and_marker_items(self):
        CounselDomain.objects.create(domain="lawfirm.example")

        response = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "tag": "privilege"},
        )

        self.assertEqual(self._ids(response), {str(self.privileged.pk)})

    def test_filtering_by_sender_domain_and_custodian(self):
        by_domain = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "sender_domain": "vendor.example"},
        )
        by_custodian = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "custodian": "alice@psd401.net"},
        )

        self.assertEqual(self._ids(by_domain), {str(self.external.pk)})
        self.assertEqual(
            self._ids(by_custodian),
            {str(self.auto_reply.pk), str(self.privileged.pk)},
        )

    def test_search_reaches_the_extracted_text_not_only_the_filename(self):
        response = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "search": "playground resurfacing"},
        )

        self.assertEqual(self._ids(response), {str(self.external.pk)})

    def _as_officer(self):
        officer = get_user_model().objects.create_user(
            "throughput-officer",
            "throughput-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(officer, [StaffRole.RECORDS_OFFICER])
        self.client.force_login(officer)
        return officer

    def test_a_records_officer_searches_by_correspondent(self):
        self._as_officer()

        response = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "search": "counsel@lawfirm"},
        )

        self.assertEqual(self._ids(response), {str(self.privileged.pk)})

    def test_a_reviewer_never_sees_or_searches_correspondent_addresses(self):
        # Searching by address is searching by identity. A reviewer works the
        # records without the requester-and-third-party addresses, exactly as
        # the request serializers already withhold requester identity; the
        # sending domain and the tags, which is what triage needs, stay.
        listed = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "search": "counsel@lawfirm"},
        )
        detail = self.client.get(
            f"/api/staff/record-items/{self.privileged.pk}/"
        ).json()

        self.assertEqual(self._ids(listed), set())
        self.assertEqual(detail["email_from"], "")
        self.assertEqual(detail["email_to"], "")
        self.assertEqual(detail["email_cc"], "")
        self.assertEqual(detail["email_sender_domain"], "lawfirm.example")

    def test_a_records_officer_sees_the_correspondent_addresses(self):
        self._as_officer()

        detail = self.client.get(
            f"/api/staff/record-items/{self.privileged.pk}/"
        ).json()

        self.assertEqual(detail["email_from"], "counsel@lawfirm.example")

    def test_a_lookalike_domain_is_not_tagged_as_district_counsel(self):
        # "lawfirm.example.attacker.test" contains the counsel domain as a
        # substring. Tagging it would assert a privilege claim nobody made.
        CounselDomain.objects.create(domain="lawfirm.example")
        lookalike = self._item(
            original_filename="Lookalike.pdf",
            email_sender_domain="lawfirm.example.attacker.test",
            email_to="impostor@lawfirm.example.attacker.test",
        )

        response = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "tag": "counsel_domain"},
        )

        self.assertNotIn(str(lookalike.pk), self._ids(response))
        self.assertEqual(self._ids(response), {str(self.privileged.pk)})

    def test_advisory_confidence_is_exposed_and_sortable_without_applying(self):
        AdvisoryRun.objects.create(
            kind=AdvisoryRun.Kind.RECORD_RESPONSIVENESS,
            state=AdvisoryRun.State.COMPLETE,
            request=self.prr,
            record_item=self.external,
            requested_by=self.reviewer,
            model_identifier="test-model",
            input_sha256="c" * 64,
            proposal={"suggested_state": "responsive", "confidence": 0.91},
            completed_at=timezone.now(),
        )

        response = self.client.get(
            "/api/staff/record-items/",
            {
                "request": self.prr.number,
                "ordering": "-confidence",
                "advisory_state": "responsive",
            },
        )

        rows = response.json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(self.external.pk)])
        self.assertAlmostEqual(rows[0]["advisory_confidence"], 0.91)
        # The proposal is still only a proposal.
        self.external.refresh_from_db()
        self.assertEqual(
            self.external.review_state, RecordItem.ReviewState.UNREVIEWED,
        )

    def test_a_reviewer_cannot_order_by_an_address_they_cannot_read(self):
        # Ordering over a hidden value reads it, one comparison at a time. The
        # two vendor records share a sending domain, so only the local part
        # could separate them -- and for a reviewer it must not.
        self._item(
            original_filename="Vendor credit.pdf",
            email_sender_domain="vendor.example",
            # Sorts before "ann@" on the address, so an ordering that reads
            # the address would put this row first ascending and last
            # descending.
            email_from="aaron@vendor.example",
            custodian="bob@psd401.net",
        )

        def order(direction):
            response = self.client.get(
                "/api/staff/record-items/",
                {
                    "request": self.prr.number,
                    "sender_domain": "vendor.example",
                    "ordering": direction,
                },
            )
            rows = response.json()["results"]
            self.assertEqual({row["email_from"] for row in rows}, {""})
            return [row["id"] for row in rows]

        # Both records share a sending domain, so once the address is out of
        # the sort there is nothing left for it to read: ascending and
        # descending return the same order. An ordering that still read the
        # address would reverse.
        self.assertEqual(order("sender"), order("-sender"))

    def test_a_records_officer_may_still_order_by_the_address(self):
        officer = get_user_model().objects.create_user(
            "throughput-officer-sort",
            "throughput-officer-sort@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(officer, [StaffRole.RECORDS_OFFICER])
        second_vendor = self._item(
            original_filename="Vendor credit.pdf",
            email_sender_domain="vendor.example",
            email_from="aaron@vendor.example",
            custodian="bob@psd401.net",
        )
        self.client.force_login(officer)

        response = self.client.get(
            "/api/staff/record-items/",
            {
                "request": self.prr.number,
                "sender_domain": "vendor.example",
                "ordering": "sender",
            },
        )

        self.assertEqual(
            [row["id"] for row in response.json()["results"]],
            [str(second_vendor.pk), str(self.external.pk)],
        )

    def test_a_nonsense_confidence_filter_is_rejected(self):
        response = self.client.get(
            "/api/staff/record-items/",
            {"request": self.prr.number, "confidence_min": "high"},
        )

        self.assertEqual(response.status_code, 400)

    def test_facets_count_the_current_filter(self):
        CounselDomain.objects.create(domain="lawfirm.example")

        response = self.client.get(
            "/api/staff/record-items/facets/",
            {"request": self.prr.number},
        )

        body = response.json()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["privilege"], 1)
        counts = {row["value"]: row["count"] for row in body["tags"]}
        self.assertEqual(counts["auto_reply"], 1)
        self.assertEqual(counts["external_sender"], 2)
        self.assertEqual(counts["counsel_domain"], 1)
        domains = {
            row["value"]: row["count"] for row in body["sender_domains"]
        }
        self.assertEqual(domains["vendor.example"], 1)
        custodians = {
            row["value"]: row["count"] for row in body["custodians"]
        }
        self.assertEqual(custodians["alice@psd401.net"], 2)

    def test_facets_narrow_with_the_filter_they_describe(self):
        response = self.client.get(
            "/api/staff/record-items/facets/",
            {"request": self.prr.number, "tag": "auto_reply"},
        )

        self.assertEqual(response.json()["total"], 1)


class BulkReviewApiTests(TestCase):
    def setUp(self):
        self.reviewer = get_user_model().objects.create_user(
            "bulk-api-reviewer",
            "bulk-api-reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, [StaffRole.REVIEWER])
        self.liaison = get_user_model().objects.create_user(
            "bulk-api-liaison",
            "bulk-api-liaison@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.liaison, [StaffRole.LIAISON])
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="requester@example.org"),
            description="Bulk review over REST.",
        )
        self.items = [
            RecordItem.objects.create(
                request=self.prr,
                original_filename=f"Automatic reply {index}.pdf",
                storage_key=f"records/{self.prr.pk}/api-{index}.pdf",
                content_length=10,
                content_type="application/pdf",
                sha256=hashlib.sha256(f"api-{index}".encode()).hexdigest(),
            )
            for index in range(3)
        ]

    def _post(self, payload):
        return self.client.post(
            "/api/staff/record-items/bulk-review/",
            payload,
            content_type="application/json",
        )

    def test_a_reviewer_marks_every_out_of_office_reply_in_one_pass(self):
        self.client.login(username=self.reviewer.username, password="pw")

        response = self._post({
            "record_item_ids": [str(item.pk) for item in self.items],
            "state": RecordItem.ReviewState.NON_RESPONSIVE,
        })

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["reviewed_count"], 3)
        self.assertEqual(
            RecordItem.objects.filter(
                review_state=RecordItem.ReviewState.NON_RESPONSIVE,
            ).count(),
            3,
        )
        self.assertEqual(
            AuditLog.objects.filter(action="record.reviewed").count(), 3
        )
        self.assertEqual(
            AuditLog.objects.filter(action="record.bulk_reviewed").count(), 1
        )

    def test_withholding_is_not_a_bulk_outcome(self):
        self.client.login(username=self.reviewer.username, password="pw")

        response = self._post({
            "record_item_ids": [str(self.items[0].pk)],
            "state": RecordItem.ReviewState.WITHHELD,
        })

        self.assertEqual(response.status_code, 400)

    def test_more_than_two_hundred_records_is_refused_at_the_boundary(self):
        import uuid

        self.client.login(username=self.reviewer.username, password="pw")

        response = self._post({
            "record_item_ids": [str(uuid.uuid4()) for _ in range(201)],
            "state": RecordItem.ReviewState.RESPONSIVE,
        })

        self.assertEqual(response.status_code, 400)

    def test_a_non_review_role_is_refused(self):
        self.client.login(username=self.liaison.username, password="pw")

        response = self._post({
            "record_item_ids": [str(self.items[0].pk)],
            "state": RecordItem.ReviewState.RESPONSIVE,
        })

        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(
            AuditLog.objects.filter(action="record.bulk_reviewed").count(), 0
        )


class CounselDomainApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            "counsel-admin", "counsel-admin@psd401.net", "pw", is_staff=True,
        )
        apply_staff_roles(self.admin, [StaffRole.ADMIN])
        self.officer = get_user_model().objects.create_user(
            "counsel-officer",
            "counsel-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, [StaffRole.RECORDS_OFFICER])

    def test_the_list_starts_empty(self):
        self.client.login(username=self.admin.username, password="pw")

        response = self.client.get("/api/staff/counsel-domains/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_an_administrator_adds_a_domain_and_it_audits(self):
        self.client.login(username=self.admin.username, password="pw")

        response = self.client.post(
            "/api/staff/counsel-domains/",
            {"domain": "LawFirm.Example", "note": "District counsel."},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["domain"], "lawfirm.example")
        self.assertTrue(
            AuditLog.objects.filter(
                action="records.counsel_domain_created",
            ).exists()
        )

    def test_a_malformed_domain_is_refused(self):
        self.client.login(username=self.admin.username, password="pw")

        response = self.client.post(
            "/api/staff/counsel-domains/",
            {"domain": "not a domain"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_a_records_officer_cannot_change_the_counsel_list(self):
        self.client.login(username=self.officer.username, password="pw")

        response = self.client.post(
            "/api/staff/counsel-domains/",
            {"domain": "lawfirm.example"},
            content_type="application/json",
        )

        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(CounselDomain.objects.count(), 0)
