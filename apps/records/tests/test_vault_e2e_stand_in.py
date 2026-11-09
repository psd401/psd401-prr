"""The e2e-only Google Vault stand-in (issue #37).

It answers Vault calls only in browser acceptance runs: the setting is
honoured only while the test-fixture switch is on, so no deployed environment
reaches it, and with either switch off every call goes to the real service.
"""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.integrations.vault import client


@override_settings(PRR_E2E_FAKE_VAULT=True, PRR_TEST_FIXTURE_SEED_ENABLED=True)
class VaultStandInTests(SimpleTestCase):
    @patch("apps.integrations.vault.client._vault_service", side_effect=AssertionError("real Vault"))
    def test_it_answers_without_reaching_vault(self, _service):
        matter = client.ensure_matter("PRR-E2E-0001", "Synthetic")
        self.assertTrue(matter.startswith("e2e-matter-"))
        self.assertEqual(matter, client.ensure_matter("PRR-E2E-0001", "Synthetic"))
        count = client.start_mail_count(matter, ["a@psd401.net", "b@psd401.net"], '"board"')
        self.assertTrue(count["done"])
        self.assertEqual(count["response"]["totalCount"], "74")
        entire = client.start_mail_count(matter, [], '"board"', entire_org=True)
        self.assertEqual(entire["response"]["totalCount"], "128")
        self.assertEqual(client.get_export(matter, "x")["status"], "IN_PROGRESS")

    def test_it_is_off_without_the_fixture_switch(self):
        for overrides in ({"PRR_TEST_FIXTURE_SEED_ENABLED": False}, {"PRR_E2E_FAKE_VAULT": False}):
            with self.subTest(**overrides), override_settings(**overrides), patch(
                "apps.integrations.vault.client._vault_service", side_effect=RuntimeError("real Vault"),
            ):
                self.assertFalse(client._e2e_stand_in())
                with self.assertRaisesMessage(RuntimeError, "real Vault"):
                    client.ensure_matter("PRR-E2E-0001", "Synthetic")
