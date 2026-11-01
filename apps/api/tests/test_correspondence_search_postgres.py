from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.identity import StaffRole, apply_staff_roles
from apps.api.resources.correspondence import CorrespondenceViewSet
from apps.correspondence.models import Correspondence
from apps.records.models import PublicRecordsRequest, Requester


@skipUnlessDBFeature("has_native_uuid_field")
class CorrespondencePostgresSearchTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            username="postgres-search-officer",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        requester = Requester.objects.create(email="postgres-search@example.org")
        self.prr = PublicRecordsRequest.objects.create(
            requester=requester,
            description="PostgreSQL message search contract.",
        )
        self.message = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Records calendar",
            received_body="The immutable cobalt schedule is attached.",
            received_at=timezone.now(),
            provider_message_id="postgres-search-message",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.officer)

    def test_websearch_syntax_uses_projected_immutable_content(self):
        response = self.client.post(
            "/api/staff/correspondence/search/",
            {
                "request": self.prr.number,
                "query": '"cobalt schedule" -missing',
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [str(self.message.pk)],
        )

        cross_field = self.client.post(
            "/api/staff/correspondence/search/",
            {"request": self.prr.number, "query": "calendar cobalt"},
            format="json",
        )
        self.assertEqual(
            [row["id"] for row in cross_field.data["results"]],
            [str(self.message.pk)],
        )

    def test_search_covers_content_after_the_first_projection_chunk(self):
        long_message = Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Long immutable reply",
            received_body=(
                "origin alpha "
                + ("ordinary filler " * 10_000)
                + "terminal zephyr forbidden"
            ),
            received_at=timezone.now(),
            provider_message_id="postgres-search-long-message",
        )

        response = self.client.post(
            "/api/staff/correspondence/search/",
            {"request": self.prr.number, "query": '"terminal zephyr"'},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [str(long_message.pk)],
        )

        cross_chunk = self.client.post(
            "/api/staff/correspondence/search/",
            {"request": self.prr.number, "query": "alpha zephyr"},
            format="json",
        )
        self.assertEqual(
            [row["id"] for row in cross_chunk.data["results"]],
            [str(long_message.pk)],
        )

        negated_other_chunk = self.client.post(
            "/api/staff/correspondence/search/",
            {"request": self.prr.number, "query": "alpha -forbidden"},
            format="json",
        )
        self.assertEqual(negated_other_chunk.data["results"], [])

    def test_gin_index_supports_the_search_predicate(self):
        view = CorrespondenceViewSet()
        endpoint_queryset = view._apply_search(
            Correspondence.objects.all(),
            "cobalt schedule",
        )
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            plan = endpoint_queryset.explain(costs=False)

        self.assertIn("corr_search_chunk_gin", plan)

    def test_body_cursor_is_complete_across_rank_and_timestamp_ties(self):
        messages = [
            Correspondence(
                request=self.prr,
                direction=Correspondence.Direction.INBOUND,
                kind=Correspondence.Kind.GENERAL,
                status=Correspondence.Status.RECEIVED,
                received_subject="Uniform ranked cursor",
                received_body="identical cobalt cursor evidence",
                received_at=timezone.now(),
                provider_message_id=f"postgres-cursor-{index}",
            )
            for index in range(61)
        ]
        tied_at = timezone.now()
        with mock.patch(
            "django.db.models.fields.timezone.now",
            return_value=tied_at,
        ):
            Correspondence.objects.bulk_create(messages)
        expected_ids = {str(row.pk) for row in messages}
        body = {
            "request": self.prr.number,
            "query": '"identical cobalt cursor evidence"',
        }

        first = self.client.post(
            "/api/staff/correspondence/search/",
            body,
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(len(first.data["results"]), 50)
        self.assertIsNotNone(first.data["next"])
        Correspondence.objects.create(
            request=self.prr,
            direction=Correspondence.Direction.INBOUND,
            kind=Correspondence.Kind.GENERAL,
            status=Correspondence.Status.RECEIVED,
            received_subject="Uniform ranked cursor",
            received_body="identical cobalt cursor evidence",
            received_at=timezone.now(),
            provider_message_id="postgres-cursor-front-insert",
        )
        second = self.client.post(
            "/api/staff/correspondence/search/",
            {**body, "cursor": first.data["next"]},
            format="json",
        )
        self.assertEqual(second.status_code, 200, second.content)
        traversed = [*first.data["results"], *second.data["results"]]
        self.assertEqual(len(traversed), len(expected_ids))
        self.assertSetEqual({row["id"] for row in traversed}, expected_ids)

        wrong_scope = self.client.post(
            "/api/staff/correspondence/search/",
            {
                **body,
                "query": "different search scope",
                "cursor": first.data["next"],
            },
            format="json",
        )
        self.assertEqual(wrong_scope.status_code, 404)
