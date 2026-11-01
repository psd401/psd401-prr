# ABOUTME: Staff API tests for the append-only audit-log resource — staff
# ABOUTME: gate, list ordering, filters, and rejection of all write verbs.
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.audit.models import AuditLog


class PaginationDocumentationContractTests(SimpleTestCase):
    def test_agent_docs_identify_keyset_feed_exceptions(self):
        documentation = " ".join(
            (Path(settings.BASE_DIR) / "docs/agentic-integration.md")
            .read_text()
            .split()
        )

        self.assertIn(
            "`audit-log` instead uses counted keyset pagination",
            documentation,
        )
        self.assertIn(
            "treat `next` as opaque and follow it unchanged",
            documentation,
        )
        self.assertIn("For the audit log", documentation)
        self.assertNotIn("the two activity feeds", documentation)
        self.assertIn(
            "The webhook event bus described below was removed by #66",
            documentation,
        )
        self.assertIn("operators and agents must not use it", documentation)
        self.assertNotIn("| `webhooks.py` |", documentation)
        self.assertNotIn(
            "Consume webhook events and react to them",
            documentation,
        )
        self.assertNotIn(
            "identity, requester, webhook, and event resources",
            documentation,
        )
        self.assertNotIn("PRR_WEBHOOK_REQUIRE_HTTPS", documentation)
        self.assertNotIn(
            "Applies to every router-registered list endpoint above.",
            documentation,
        )


class AuditLogApiTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True)
        self.client.login(username="officer", password="pw")

    def _log(self, action, target="", **detail):
        return AuditLog.objects.create(action=action, target=target, detail=detail)

    def _log_at(self, at, action, target="", **detail):
        with patch("django.utils.timezone.now", return_value=at):
            return self._log(action, target=target, **detail)

    @staticmethod
    def _rows(response):
        payload = response.json()
        return payload["results"] if isinstance(payload, dict) else payload

    def test_staff_endpoint_refuses_anonymous(self):
        self.client.logout()
        r = self.client.get("/api/staff/audit-log/")
        self.assertEqual(r.status_code, 403)

    def test_staff_endpoint_refuses_non_staff(self):
        get_user_model().objects.create_user(
            "civilian", "c@example.com", "pw", is_staff=False)
        self.client.logout()
        self.client.login(username="civilian", password="pw")
        r = self.client.get("/api/staff/audit-log/")
        self.assertEqual(r.status_code, 403)

    def test_list_orders_newest_first(self):
        old = self._log_at(
            timezone.now() - timedelta(days=5),
            "request.created",
            target="PRR-2026-0001",
        )
        newest = self._log("request.updated", target="PRR-2026-0002")
        r = self.client.get("/api/staff/audit-log/")
        self.assertEqual(r.status_code, 200)
        rows = self._rows(r)
        self.assertEqual(rows[0]["id"], newest.id)
        self.assertEqual(rows[-1]["id"], old.id)

    def test_pagination_is_stable_when_new_rows_arrive_and_timestamps_tie(self):
        tied_at = timezone.now() - timedelta(minutes=1)
        originals = [
            self._log_at(tied_at, "request.updated", target=f"PRR-{index:04d}")
            for index in range(51)
        ]

        first = self.client.get("/api/staff/audit-log/")
        self.assertEqual(first.status_code, 200)
        first_payload = first.json()
        first_ids = [row["id"] for row in first_payload["results"]]
        self.assertEqual(first_ids, [entry.id for entry in reversed(originals)][0:50])

        inserted = self._log_at(
            tied_at + timedelta(seconds=1),
            "request.created",
            target="PRR-NEW",
        )
        next_url = urlsplit(first_payload["next"])
        second = self.client.get(f"{next_url.path}?{next_url.query}")
        self.assertEqual(second.status_code, 200)
        second_ids = [row["id"] for row in second.json()["results"]]

        combined = first_ids + second_ids
        original_ids = {entry.id for entry in originals}
        self.assertEqual(set(combined), original_ids)
        self.assertEqual(len(combined), len(original_ids))
        self.assertNotIn(inserted.id, combined)

    def test_legacy_page_number_is_rejected_instead_of_replaying_page_one(self):
        self._log("request.created", target="PRR-2026-0001")

        response = self.client.get("/api/staff/audit-log/?page=2")

        self.assertEqual(response.status_code, 404)
        self.assertIn("follow the next link", response.json()["detail"])

    def test_retrieve_serializes_all_fields(self):
        log = AuditLog.objects.create(
            action="vault.export.created",
            target="PRR-2026-0009",
            actor=self.officer,
            detail={"note": "x"},
        )
        r = self.client.get(f"/api/staff/audit-log/{log.id}/")
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertEqual(payload["action"], "vault.export.created")
        self.assertEqual(payload["target"], "PRR-2026-0009")
        self.assertEqual(payload["detail"], {"note": "x"})
        self.assertIn("at", payload)
        self.assertIn("actor", payload)
        self.assertEqual(payload["actor_username"], "officer")
        self.assertIn("ip_address", payload)

    def test_filter_by_action_icontains(self):
        self._log("request.created", target="PRR-2026-0001")
        self._log("request.updated", target="PRR-2026-0002")
        self._log("vault.export.created", target="PRR-2026-0003")
        r = self.client.get("/api/staff/audit-log/?action=request")
        rows = self._rows(r)
        self.assertEqual({row["action"] for row in rows},
                         {"request.created", "request.updated"})

    def test_filter_by_target_icontains(self):
        self._log("request.created", target="PRR-2026-0001")
        self._log("request.updated", target="PRR-2026-0099")
        r = self.client.get("/api/staff/audit-log/?target=0001")
        rows = self._rows(r)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "PRR-2026-0001")

    def test_filter_by_since(self):
        old = self._log_at(
            timezone.now() - timedelta(days=10),
            "request.created",
            target="PRR-2026-0001",
        )
        recent = self._log("request.updated", target="PRR-2026-0002")
        cutoff = (date.today() - timedelta(days=1)).isoformat()
        r = self.client.get(f"/api/staff/audit-log/?since={cutoff}")
        rows = self._rows(r)
        ids = {row["id"] for row in rows}
        self.assertIn(recent.id, ids)
        self.assertNotIn(old.id, ids)

    def test_write_methods_rejected(self):
        log = self._log("request.created", target="PRR-2026-0001")
        detail_url = f"/api/staff/audit-log/{log.id}/"
        list_url = "/api/staff/audit-log/"
        self.assertEqual(
            self.client.post(list_url, {"action": "hack"},
                             content_type="application/json").status_code, 405)
        self.assertEqual(
            self.client.put(detail_url, {"action": "hack"},
                            content_type="application/json").status_code, 405)
        self.assertEqual(
            self.client.patch(detail_url, {"action": "hack"},
                              content_type="application/json").status_code, 405)
        self.assertEqual(self.client.delete(detail_url).status_code, 405)
