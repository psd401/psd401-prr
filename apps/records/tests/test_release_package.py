"""One-step release package (issue #47).

"Build package" chains what the Releases tab used to ask for in three steps:
open an installment from every release-ready record, generate the exemption
log when one is needed, and prepare the package. It stops at the first
blocker with a reason and the tab that fixes it, before changing anything.
Release and send stays a separate, human-only action.
"""

import hashlib
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.records.models import (
    ExemptionCitation,
    Installment,
    PublicRecordsRequest,
    RecordItem,
    Requester,
    RequestStatus,
)
from apps.records.release_package import ReleasePackageBlocked, build_release_package

from .object_store_fakes import FakeObjectClient


class _Fixture(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create(
            username="release-officer", email="release-officer@psd401.net", is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.prr = PublicRecordsRequest.objects.create(
            requester=Requester.objects.create(email="release@example.org"),
            description="Board records.",
        )
        self.objects = {}
        self.ready = [self._item(f"minutes-{n}.pdf") for n in range(2)]
        self.unreviewed = self._item("draft.pdf", state=RecordItem.ReviewState.UNREVIEWED)
        self.client_store = FakeObjectClient(self.objects)

    def _item(self, name, state=RecordItem.ReviewState.RESPONSIVE):
        payload = f"%PDF-1.7\n{name}".encode()
        key = f"records/{self.prr.pk}/{name}"
        self.objects[key] = payload
        reviewed = state != RecordItem.ReviewState.UNREVIEWED
        return RecordItem.objects.create(
            request=self.prr, original_filename=name, storage_key=key,
            sha256=hashlib.sha256(payload).hexdigest(), content_length=len(payload),
            content_type="application/pdf", review_state=state,
            reviewed_by=self.officer if reviewed else None,
            reviewed_at=timezone.now() if reviewed else None,
        )



@override_settings(OBJECT_STORE={"bucket": "records"})
class BuildReleasePackageTests(_Fixture):
    def _build(self, actor=None):
        return build_release_package(
            self.prr.number, actor=actor or self.officer, _client=self.client_store,
        )

    def test_it_packages_every_release_ready_record_in_one_action(self):
        installment = self._build()

        self.assertEqual(set(installment.items.all()), set(self.ready))
        self.assertIsNotNone(installment.package_prepared_at)
        self.assertEqual(installment.exemption_log_storage_key, "")
        self.assertIsNone(installment.released_at)

    def test_a_withheld_record_gets_its_exemption_log_first(self):
        exemption = ExemptionCitation.objects.create(
            citation="RCW 42.56.230(1)", short_name="Personal information",
        )
        RecordItem.objects.filter(pk=self.ready[0].pk).update(
            review_state=RecordItem.ReviewState.WITHHELD, withholding_exemption=exemption,
        )

        installment = self._build()

        self.assertNotEqual(installment.exemption_log_storage_key, "")
        self.assertIsNotNone(installment.package_prepared_at)

    def test_building_again_reuses_the_open_package(self):
        first = self._build()
        puts = len(self.client_store.put_calls)

        again = self._build()

        self.assertEqual(again.pk, first.pk)
        self.assertEqual(Installment.objects.count(), 1)
        self.assertEqual(len(self.client_store.put_calls), puts)

    def test_an_unprepared_installment_is_finished_rather_than_duplicated(self):
        open_one = Installment.objects.create(request=self.prr, sequence=1)
        open_one.items.add(self.ready[0])

        installment = self._build()

        self.assertEqual(installment.pk, open_one.pk)
        self.assertEqual(list(installment.items.all()), [self.ready[0]])
        self.assertIsNotNone(installment.package_prepared_at)

    def test_no_ready_records_points_at_review(self):
        RecordItem.objects.filter(pk__in=[i.pk for i in self.ready]).update(
            review_state=RecordItem.ReviewState.UNREVIEWED, reviewed_by=None, reviewed_at=None,
        )
        with self.assertRaises(ReleasePackageBlocked) as caught:
            self._build()
        self.assertEqual(caught.exception.code, "no_ready_records")
        self.assertEqual(caught.exception.tab, "review")
        self.assertFalse(Installment.objects.exists())

    def test_an_open_production_deposit_stops_before_anything_changes(self):
        gate = SimpleNamespace(amount=Decimal("12.50"))
        with patch("apps.records.release_package.open_deposit_gate", return_value=gate), \
                self.assertRaises(ReleasePackageBlocked) as caught:
            self._build()
        self.assertEqual(caught.exception.code, "deposit_required")
        self.assertEqual(caught.exception.tab, "releases")
        self.assertIn("$12.50", str(caught.exception.message))
        self.assertFalse(Installment.objects.exists())

    def test_a_closed_request_points_at_the_overview(self):
        PublicRecordsRequest.objects.filter(pk=self.prr.pk).update(status=RequestStatus.CLOSED_WITHDRAWN)
        with self.assertRaises(ReleasePackageBlocked) as caught:
            self._build()
        self.assertEqual(caught.exception.code, "request_closed")
        self.assertEqual(caught.exception.tab, "overview")

    def test_only_a_records_officer_builds(self):
        reviewer = get_user_model().objects.create(username="rev", email="rev@psd401.net", is_staff=True)
        apply_staff_roles(reviewer, {StaffRole.REVIEWER})
        with self.assertRaises(PermissionDenied):
            self._build(actor=reviewer)
        self.assertFalse(Installment.objects.exists())


@override_settings(OBJECT_STORE={"bucket": "records"})
class BuildReleasePackageApiTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(self.officer)

    def test_the_api_builds_and_returns_the_installment(self):
        with patch("apps.records.storage._object_store_client", return_value=self.client_store):
            response = self.api.post(f"/api/staff/requests/{self.prr.number}/release-package/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["item_count"], 2)
        self.assertIsNotNone(response.json()["package_prepared_at"])

    def test_the_api_returns_the_blocker_and_where_to_fix_it(self):
        RecordItem.objects.filter(request=self.prr).delete()
        response = self.api.post(f"/api/staff/requests/{self.prr.number}/release-package/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["blocker"], {"code": "no_ready_records", "tab": "review"})
        self.assertTrue(body["detail"])
