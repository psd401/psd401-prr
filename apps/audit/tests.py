# ABOUTME: Verifies the litigation-ready, deterministic audit backup export.
# ABOUTME: Keeps the export complete, ordered, canonical, and PII-minimal.
import io
import json

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.audit.models import AuditLog


class ExportAuditLogCommandTests(TestCase):
    def test_exports_canonical_json_lines_in_primary_key_order(self):
        actor = get_user_model().objects.create_user(
            username="backup-operator",
            email="operator@example.invalid",
        )
        first = AuditLog.objects.create(
            actor=actor,
            action="request.created",
            target="PRR-2026-0001",
            detail={"z": 2, "a": 1},
            ip_address="192.0.2.10",
        )
        second = AuditLog.objects.create(
            action="request.received",
            target="PRR-2026-0002",
            detail={},
        )
        output = io.StringIO()

        call_command("export_audit_log", stdout=output)

        lines = output.getvalue().splitlines()
        payloads = [json.loads(line) for line in lines]
        self.assertEqual([payload["id"] for payload in payloads], [first.pk, second.pk])
        self.assertEqual(payloads[0]["actor_id"], actor.pk)
        self.assertNotIn("operator@example.invalid", output.getvalue())
        self.assertEqual(payloads[0]["detail"], {"a": 1, "z": 2})
        self.assertTrue(payloads[0]["at"].endswith("Z"))
        for line, payload in zip(lines, payloads, strict=True):
            self.assertEqual(
                line,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
