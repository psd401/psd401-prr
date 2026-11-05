"""The #43 MCP client methods: record, cluster, and advisory reads shaped to
triage fields, and decision batches proposed only with a personal token."""

from unittest.mock import patch

from django.test import SimpleTestCase

from integrations.mcp.client import PRRClient

PERSONAL = "prr_pat_example"
ITEM_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
BATCH_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def client(token=PERSONAL):
    return PRRClient("https://prr.example.org", token)


class RecordReadTests(SimpleTestCase):
    def test_record_rows_carry_triage_fields_only(self):
        row = {
            "id": ITEM_ID, "original_filename": "minutes.pdf", "review_state": "unreviewed",
            "email_from": "parent@example.org", "email_to": ["teacher@psd401.net"],
            "sha256": "f" * 64, "advisory_suggested_state": "responsive", "advisory_confidence": 0.9,
        }
        with patch.object(PRRClient, "_get", return_value={"count": 51, "next": "x", "results": [row]}) as get:
            result = client().list_record_items("PRR-2026-0042", review_state="unreviewed")
        get.assert_called_once_with(
            "/api/staff/record-items/", {"request": "PRR-2026-0042", "review_state": "unreviewed"},
        )
        shaped = result["results"][0]
        self.assertEqual(shaped["advisory_suggested_state"], "responsive")
        self.assertNotIn("email_from", shaped)
        self.assertNotIn("email_to", shaped)
        self.assertEqual(result["count"], 51)
        self.assertEqual(result["next_page"], 2)

    def test_clusters_keep_ids_and_majority_but_not_samples(self):
        payload = {"group_by": "thread", "cluster_count": 1, "clusters": [
            {"key": "t1", "label": "Re: minutes", "size": 3, "ai_majority": {"label": "responsive", "count": 3},
             "samples": [{"ocr_text": "secret"}], "record_item_ids": [ITEM_ID]},
        ]}
        with patch.object(PRRClient, "_get", return_value=payload):
            result = client().list_record_clusters("PRR-2026-0042")
        self.assertEqual(result["clusters"][0]["record_item_ids"], [ITEM_ID])
        self.assertNotIn("samples", result["clusters"][0])

    def test_advisories_drop_the_model_rationale(self):
        responsiveness = {"latest": {
            "state": "complete", "model_identifier": "m",
            "proposal": {"suggested_state": "non_responsive", "confidence": 0.8,
                         "matched_terms": ["board"], "rationale": "Quotes a student's name."},
        }}
        redactions = {"latest": {
            "state": "complete", "model_identifier": "m",
            "proposal": {"candidates": [{"exemption_id": 3, "pattern": "phone", "confidence": 0.9,
                                         "rationale": "A cell number.", "span_ids": ["p1s1"]}]},
        }}
        with patch.object(PRRClient, "_get", side_effect=[responsiveness, redactions]):
            result = client().get_advisories(ITEM_ID)
        self.assertEqual(result["responsiveness"]["suggested_state"], "non_responsive")
        self.assertEqual(result["redactions"]["candidates"], [{"exemption_id": 3, "pattern": "phone", "confidence": 0.9}])
        self.assertNotIn("rationale", str(result))

    def test_advisories_without_runs_and_bad_ids(self):
        with patch.object(PRRClient, "_get", return_value={"latest": None}):
            result = client().get_advisories(ITEM_ID)
        self.assertIsNone(result["responsiveness"])
        self.assertIsNone(result["redactions"])
        with self.assertRaises(ValueError):
            client().get_advisories("not-a-uuid")


class DecisionBatchToolTests(SimpleTestCase):
    def test_proposing_needs_a_personal_token(self):
        with self.assertRaises(PermissionError):
            client(token="shared-readonly").propose_decision_batch("review", {}, "Evidence.")

    def test_a_proposal_posts_and_says_a_person_decides(self):
        with patch.object(PRRClient, "_post", return_value={"id": BATCH_ID, "state": "proposed"}) as post:
            result = client().propose_decision_batch(
                "review", {"record_item_ids": [ITEM_ID], "state": "responsive"}, "Board minutes.",
            )
        post.assert_called_once_with("/api/staff/decision-batches/", {
            "kind": "review", "payload": {"record_item_ids": [ITEM_ID], "state": "responsive"},
            "evidence_summary": "Board minutes.",
        })
        self.assertFalse(result["applied"])
        self.assertTrue(result["human_gate"]["required"])

    def test_a_proposal_is_validated_before_it_is_sent(self):
        for kind, payload, evidence in (("approve", {}, "x"), ("review", [], "x"), ("review", {}, " ")):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                client().propose_decision_batch(kind, payload, evidence)  # type: ignore[arg-type]

    def test_getting_one_batch_or_a_filtered_list(self):
        with patch.object(PRRClient, "_get", return_value={}) as get:
            client().get_decision_batch(batch_id=BATCH_ID)
            client().get_decision_batch(request_number="PRR-2026-0042", state="proposed")
            client().get_decision_batch()
        self.assertEqual(get.call_args_list[0].args, (f"/api/staff/decision-batches/{BATCH_ID}/",))
        self.assertEqual(
            get.call_args_list[1].args,
            ("/api/staff/decision-batches/", {"request": "PRR-2026-0042", "state": "proposed"}),
        )
        self.assertEqual(get.call_args_list[2].args, ("/api/staff/decision-batches/", None))
        with self.assertRaises(ValueError):
            client().get_decision_batch(state="pending")
        with self.assertRaises(ValueError):
            client().get_decision_batch(batch_id="nope")
