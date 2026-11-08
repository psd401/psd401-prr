"""The Vault client speaks Google Vault v1's request shapes for every source.

Gmail, Drive, and Chat each have their own count, export, and hold calls
behind one interface (``ensure_count_export``, ``ensure_source_export``,
``ensure_source_hold``). These tests pin the exact
bodies sent to Google; nothing here reaches Google.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.integrations.vault import client


def _service():
    svc = MagicMock()
    svc.matters().exports().list().execute.return_value = {"exports": []}
    svc.matters().holds().list().execute.return_value = {"holds": []}
    svc.matters().exports().create().execute.return_value = {"id": "export-1"}
    svc.matters().holds().create().execute.return_value = {"holdId": "hold-1"}
    svc.matters().count().execute.return_value = {"name": "operations/c-1"}
    svc.reset_mock()
    return svc


class SourceQueryShapeTests(SimpleTestCase):
    def test_drive_query_searches_named_accounts_and_shared_drives(self):
        query = client.source_query(
            "drive", ["a@psd401.net"], '"bus route"',
            start_time="2026-01-01T08:00:00Z", end_time="2026-02-01T08:00:00Z",
        )
        self.assertEqual(query, {
            "corpus": "DRIVE",
            "dataScope": "ALL_DATA",
            "method": "ACCOUNT",
            "accountInfo": {"emails": ["a@psd401.net"]},
            "terms": '"bus route"',
            "driveOptions": {"sharedDrivesOption": "INCLUDED"},
            "startTime": "2026-01-01T08:00:00Z",
            "endTime": "2026-02-01T08:00:00Z",
        })

    def test_chat_query_includes_spaces_the_accounts_were_in(self):
        query = client.source_query("chat", ["a@psd401.net"], "bus")
        self.assertEqual(query["corpus"], "HANGOUTS_CHAT")
        self.assertEqual(query["method"], "ACCOUNT")
        self.assertEqual(query["hangoutsChatOptions"], {"includeRooms": True})
        self.assertNotIn("startTime", query)

    def test_district_wide_drive_and_chat_search_the_staff_org_unit(self):
        for source in ("drive", "chat"):
            with self.subTest(source=source):
                query = client.source_query(
                    source, [], "bus", entire_org=True, org_unit_id="id:03staff",
                )
                self.assertEqual(query["method"], "ORG_UNIT")
                self.assertEqual(query["orgUnitInfo"], {"orgUnitId": "id:03staff"})
                self.assertNotIn("accountInfo", query)

    def test_district_wide_drive_without_an_org_unit_is_refused(self):
        with self.assertRaises(client.StaffOrgUnitMissing):
            client.source_query("drive", [], "bus", entire_org=True, org_unit_id="")

    def test_gmail_keeps_the_entire_org_method(self):
        query = client.source_query("mail", [], "bus", entire_org=True, org_unit_id="id:x")
        self.assertEqual(query["corpus"], "MAIL")
        self.assertEqual(query["method"], "ENTIRE_ORG")

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError):
            client.source_query("voice", ["a@psd401.net"], "bus")


@patch("apps.integrations.vault.client.audit")
class SourceCallTests(SimpleTestCase):
    def test_count_only_export_is_reused_by_name(self, _audit):
        svc = _service()
        svc.matters().exports().list().execute.return_value = {"exports": [
            {"id": "old", "name": "count drive", "status": "IN_PROGRESS"},
        ]}
        with patch.object(client, "_vault_service", return_value=svc):
            export_id = client.ensure_count_export(
                "drive", "matter-1", ["a@psd401.net"], "bus", name="count drive",
            )
        self.assertEqual(export_id, "old")
        svc.matters().exports().create.assert_not_called()

    def test_chat_count_only_export_body(self, _audit):
        svc = _service()
        with patch.object(client, "_vault_service", return_value=svc):
            export_id = client.ensure_count_export(
                "chat", "matter-1", ["a@psd401.net"], "bus", name="count chat",
            )
        self.assertEqual(export_id, "export-1")
        _, kwargs = svc.matters().exports().create.call_args
        self.assertEqual(kwargs["body"]["name"], "count chat")
        self.assertEqual(kwargs["body"]["query"]["corpus"], "HANGOUTS_CHAT")
        self.assertEqual(
            kwargs["body"]["exportOptions"],
            {"hangoutsChatOptions": {"exportFormat": "MBOX"}},
        )
        _audit.assert_called_once()
        self.assertEqual(_audit.call_args.args[0], "vault.count_export.created")

    def test_drive_collection_export_body(self, _audit):
        svc = _service()
        with patch.object(client, "_vault_service", return_value=svc):
            client.ensure_source_export(
                "drive", "matter-1", ["a@psd401.net"], "bus", name="PRR drive",
            )
        _, kwargs = svc.matters().exports().create.call_args
        self.assertEqual(kwargs["body"]["query"]["corpus"], "DRIVE")
        self.assertEqual(
            kwargs["body"]["exportOptions"],
            {"driveOptions": {"includeAccessInfo": False}},
        )
        self.assertEqual(_audit.call_args.args[0], "vault.export.created")

    def test_mail_collection_export_keeps_its_own_method(self, _audit):
        with patch.object(client, "ensure_mail_export", return_value="e") as mail:
            self.assertEqual(
                client.ensure_source_export("mail", "m", ["a@psd401.net"], "bus", name="n"),
                "e",
            )
        mail.assert_called_once()

    def test_mail_hold_keeps_its_own_method(self, _audit):
        with patch.object(client, "ensure_mail_hold", return_value="h") as mail:
            self.assertEqual(
                client.ensure_source_hold("mail", "m", ["a@psd401.net"], name="n"), "h",
            )
        mail.assert_called_once_with("m", ["a@psd401.net"], name="n")

    def test_drive_and_chat_holds(self, _audit):
        expected = {
            "drive": ("DRIVE", {"driveQuery": {"includeSharedDriveFiles": True}}),
            "chat": ("HANGOUTS_CHAT", {"hangoutsChatQuery": {"includeRooms": True}}),
        }
        for source, (corpus, query) in expected.items():
            with self.subTest(source=source):
                svc = _service()
                with patch.object(client, "_vault_service", return_value=svc):
                    hold_id = client.ensure_source_hold(
                        source, "matter-1", ["a@psd401.net"], name=f"hold {source}",
                    )
                self.assertEqual(hold_id, "hold-1")
                _, kwargs = svc.matters().holds().create.call_args
                self.assertEqual(kwargs["body"], {
                    "name": f"hold {source}",
                    "corpus": corpus,
                    "accounts": [{"email": "a@psd401.net"}],
                    "query": query,
                })

    def test_an_existing_named_hold_is_reused(self, _audit):
        svc = _service()
        svc.matters().holds().list().execute.return_value = {"holds": [
            {"holdId": "h-9", "name": "hold drive"},
        ]}
        with patch.object(client, "_vault_service", return_value=svc):
            hold_id = client.ensure_source_hold("drive", "m", ["a@psd401.net"], name="hold drive")
        self.assertEqual(hold_id, "h-9")
        svc.matters().holds().create.assert_not_called()

    def test_delete_export_is_audited(self, _audit):
        svc = _service()
        with patch.object(client, "_vault_service", return_value=svc):
            client.delete_export("matter-1", "export-1")
        _, kwargs = svc.matters().exports().delete.call_args
        self.assertEqual(kwargs, {"matterId": "matter-1", "exportId": "export-1"})
        self.assertEqual(_audit.call_args.args[0], "vault.count_export.deleted")

    def test_delete_export_tolerates_an_export_already_gone(self, _audit):
        from googleapiclient.errors import HttpError

        svc = _service()
        svc.matters().exports().delete().execute.side_effect = HttpError(
            MagicMock(status=404), b"gone",
        )
        with patch.object(client, "_vault_service", return_value=svc):
            client.delete_export("matter-1", "export-1")
        self.assertEqual(_audit.call_args.args[0], "vault.count_export.deleted")

    def test_delete_export_raises_other_errors(self, _audit):
        from googleapiclient.errors import HttpError

        svc = _service()
        svc.matters().exports().delete().execute.side_effect = HttpError(
            MagicMock(status=403), b"denied",
        )
        with (
            patch.object(client, "_vault_service", return_value=svc),
            self.assertRaises(HttpError),
        ):
            client.delete_export("matter-1", "export-1")


class ExportTotalTests(SimpleTestCase):
    def test_reads_total_artifact_count(self):
        self.assertEqual(
            client.export_artifact_total({"stats": {"totalArtifactCount": "42"}}), 42,
        )

    def test_missing_or_invalid_total_is_none(self):
        for export in ({}, {"stats": {}}, {"stats": {"totalArtifactCount": "x"}},
                       {"stats": {"totalArtifactCount": "-1"}}, {"stats": "bad"}):
            with self.subTest(export=export):
                self.assertIsNone(client.export_artifact_total(export))


@override_settings(PRR_E2E_FAKE_VAULT=True, PRR_TEST_FIXTURE_SEED_ENABLED=True)
class StandInSourceTests(SimpleTestCase):
    @patch("apps.integrations.vault.client._vault_service", side_effect=AssertionError("real Vault"))
    def test_stand_in_answers_every_source_without_vault(self, _service):
        for source in ("drive", "chat"):
            with self.subTest(source=source):
                count_export = client.ensure_count_export(
                    source, "m", ["a@psd401.net"], "b", name="n",
                )
                export = client.get_export("m", count_export)
                self.assertEqual(export["status"], "COMPLETED")
                self.assertGreater(client.export_artifact_total(export) or 0, 0)
                self.assertTrue(
                    client.ensure_source_export(source, "m", ["a@psd401.net"], "b", name="n")
                )
                self.assertTrue(
                    client.ensure_source_hold(source, "m", ["a@psd401.net"], name="n")
                )
        client.delete_export("m", "e")
