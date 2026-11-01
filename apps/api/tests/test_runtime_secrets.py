import json
import os
import subprocess
import sys

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.runtime_secrets import (
    load_runtime_environment,
    read_runtime_secret,
    resolve_runtime_secret,
)


class RuntimeSecretTests(SimpleTestCase):
    def test_inline_value_does_not_call_aws(self):
        def unexpected_client_factory(*args, **kwargs):
            self.fail("AWS must not be called when an inline value is configured")

        value = resolve_runtime_secret(
            inline_value="local-development-secret",
            secret_id="",
            region="us-west-2",
            client_factory=unexpected_client_factory,
        )

        self.assertEqual(value, "local-development-secret")

    def test_loads_current_secret_string_from_aws(self):
        calls = []

        class FakeSecretsManagerClient:
            def get_secret_value(self, **kwargs):
                calls.append(kwargs)
                return {"SecretString": "production-secret"}

        def client_factory(service_name, **kwargs):
            calls.append((service_name, kwargs))
            return FakeSecretsManagerClient()

        value = resolve_runtime_secret(
            inline_value="",
            secret_id="psd401-prr/prod/google-oidc-client-secret",
            region="us-west-2",
            client_factory=client_factory,
        )

        self.assertEqual(value, "production-secret")
        self.assertEqual(
            calls,
            [
                ("secretsmanager", {"region_name": "us-west-2"}),
                {
                    "SecretId": "psd401-prr/prod/google-oidc-client-secret",
                    "VersionStage": "AWSCURRENT",
                },
            ],
        )

    def test_rejects_ambiguous_inline_and_aws_configuration(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            "Configure either PRR_GOOGLE_OIDC_CLIENT_SECRET or "
            "PRR_GOOGLE_OIDC_CLIENT_SECRET_ID, not both.",
        ):
            resolve_runtime_secret(
                inline_value="inline-secret",
                secret_id="aws-secret-id",
                region="us-west-2",
            )

    def test_retrieval_failure_does_not_expose_secret_material(self):
        class FailingSecretsManagerClient:
            def get_secret_value(self, **kwargs):
                raise RuntimeError("upstream failure containing sensitive-value")

        with self.assertRaises(ImproperlyConfigured) as caught:
            resolve_runtime_secret(
                inline_value="",
                secret_id="psd401-prr/prod/google-oidc-client-secret",
                region="us-west-2",
                client_factory=lambda *args, **kwargs: FailingSecretsManagerClient(),
            )

        self.assertEqual(
            str(caught.exception),
            "Unable to load the Google OIDC client secret from AWS Secrets Manager.",
        )
        self.assertNotIn("sensitive-value", str(caught.exception))

    def test_rejects_missing_or_empty_secret_string(self):
        for response in ({}, {"SecretString": ""}, {"SecretBinary": b"secret"}):
            with self.subTest(response=response):

                class FakeSecretsManagerClient:
                    payload = response

                    def get_secret_value(self, **kwargs):
                        return self.payload

                with self.assertRaisesMessage(
                    ImproperlyConfigured,
                    "AWS Secrets Manager returned an empty Google OIDC client secret.",
                ):
                    resolve_runtime_secret(
                        inline_value="",
                        secret_id="psd401-prr/prod/google-oidc-client-secret",
                        region="us-west-2",
                        client_factory=lambda *args, **kwargs: (
                            FakeSecretsManagerClient()
                        ),
                    )


# The deployed instance's Django signing key and settings-store key, read at
# process start through the instance role (psd-dev-standards standards/04
# rule 15a). The values here are fakes; nothing in these tests reaches AWS.
SECRET_ID = "psd401-prr/prod/runtime"
SIGNING_KEY = "s" * 64


def _secret_client(secret_string, calls=None):
    class FakeSecretsManagerClient:
        def get_secret_value(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            return {"SecretString": secret_string}

    def client_factory(service_name, **kwargs):
        if calls is not None:
            calls.append((service_name, kwargs))
        return FakeSecretsManagerClient()

    return client_factory


def _runtime_secret(**values):
    return json.dumps(
        {"DJANGO_SECRET_KEY": SIGNING_KEY, "PRR_SETTINGS_ENCRYPTION_KEY": "", **values}
    )


class RuntimeEnvironmentSecretTests(SimpleTestCase):
    def test_without_a_secret_id_aws_is_never_called(self):
        environ = {"DJANGO_SECRET_KEY": "local-only"}

        def unexpected_client_factory(*args, **kwargs):
            self.fail("AWS must not be called without PRR_RUNTIME_SECRET_ID")

        values = read_runtime_secret(environ, client_factory=unexpected_client_factory)

        self.assertEqual(values, {})
        self.assertEqual(environ, {"DJANGO_SECRET_KEY": "local-only"})

    def test_loads_both_keys_from_the_current_version(self):
        calls = []
        environ = {"PRR_RUNTIME_SECRET_ID": SECRET_ID, "AWS_DEFAULT_REGION": "us-west-2"}

        values = read_runtime_secret(
            environ, client_factory=_secret_client(_runtime_secret(), calls)
        )

        self.assertEqual(values["DJANGO_SECRET_KEY"], SIGNING_KEY)
        # Blank stays blank: production derives the settings key from the
        # signing key, and generating one here would strand every stored secret.
        self.assertEqual(values["PRR_SETTINGS_ENCRYPTION_KEY"], "")
        # Returned, never written back: a child process (a media tool, a
        # nested manage.py) inherits nothing, and so cannot trip the inline
        # refusal or carry the secret further.
        self.assertNotIn("DJANGO_SECRET_KEY", environ)
        self.assertNotIn("PRR_SETTINGS_ENCRYPTION_KEY", environ)
        self.assertEqual(
            calls,
            [
                ("secretsmanager", {"region_name": "us-west-2"}),
                {"SecretId": SECRET_ID, "VersionStage": "AWSCURRENT"},
            ],
        )

    def test_a_secret_id_without_a_region_is_refused(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "AWS_DEFAULT_REGION"):
            read_runtime_secret(
                {"PRR_RUNTIME_SECRET_ID": SECRET_ID},
                client_factory=_secret_client(_runtime_secret()),
            )

    def test_provider_failure_names_no_value_and_chains_nothing(self):
        class FailingClient:
            def get_secret_value(self, **kwargs):
                raise RuntimeError("upstream failure containing sensitive-value")

        with self.assertRaises(ImproperlyConfigured) as caught:
            load_runtime_environment(
                secret_id=SECRET_ID,
                region="us-west-2",
                client_factory=lambda *args, **kwargs: FailingClient(),
            )

        self.assertNotIn("sensitive-value", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_malformed_secret_strings_are_refused(self):
        for secret_string in (None, "", "not json", "[]", '"text"'):
            with self.subTest(secret_string=secret_string):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    load_runtime_environment(
                        secret_id=SECRET_ID,
                        region="us-west-2",
                        client_factory=_secret_client(secret_string),
                    )
                if secret_string:
                    self.assertNotIn(secret_string, str(caught.exception))

    def test_the_key_set_must_be_exact(self):
        cases = {
            "missing signing key": json.dumps({"PRR_SETTINGS_ENCRYPTION_KEY": ""}),
            "missing settings key": json.dumps({"DJANGO_SECRET_KEY": SIGNING_KEY}),
            "empty signing key": _runtime_secret(DJANGO_SECRET_KEY=""),
            "unknown key": _runtime_secret(EXTRA="sensitive-value"),
            "non-string value": _runtime_secret(PRR_SETTINGS_ENCRYPTION_KEY=None),
        }
        for name, secret_string in cases.items():
            with self.subTest(name):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    load_runtime_environment(
                        secret_id=SECRET_ID,
                        region="us-west-2",
                        client_factory=_secret_client(secret_string),
                    )
                self.assertNotIn(SIGNING_KEY, str(caught.exception))
                self.assertNotIn("sensitive-value", str(caught.exception))

    def test_any_inline_key_is_refused_by_name_only(self):
        """Once a deployment reads the runtime secret, neither key may also
        live in its environment: equal or not, an inline copy is a secret in
        a host file again (production's .env.app was scrubbed 2026-10-14)."""
        for key, inline in (
            ("DJANGO_SECRET_KEY", SIGNING_KEY),
            ("DJANGO_SECRET_KEY", "different-inline-signing-key"),
            ("PRR_SETTINGS_ENCRYPTION_KEY", "inline-settings-key"),
        ):
            with self.subTest(key=key, equal=inline == SIGNING_KEY):
                environ = {
                    "PRR_RUNTIME_SECRET_ID": SECRET_ID,
                    "AWS_DEFAULT_REGION": "us-west-2",
                    key: inline,
                }
                with self.assertRaises(ImproperlyConfigured) as caught:
                    read_runtime_secret(
                        environ, client_factory=_secret_client(_runtime_secret())
                    )
                self.assertIn(key, str(caught.exception))
                self.assertNotIn(inline, str(caught.exception))
                self.assertNotIn(SIGNING_KEY, str(caught.exception))
                self.assertEqual(environ[key], inline)

    def test_a_blank_inline_key_is_not_a_copy(self):
        environ = {
            "PRR_RUNTIME_SECRET_ID": SECRET_ID,
            "AWS_DEFAULT_REGION": "us-west-2",
            "PRR_SETTINGS_ENCRYPTION_KEY": "",
        }

        values = read_runtime_secret(
            environ, client_factory=_secret_client(_runtime_secret())
        )

        self.assertEqual(values["DJANGO_SECRET_KEY"], SIGNING_KEY)


class RuntimeEnvironmentSettingsWiringTests(SimpleTestCase):
    """``override_settings`` would pass even if nothing read the secret; these
    import the real settings module in a fresh interpreter."""

    def _import_settings(self, secret_string=None, fail=False):
        script = (
            "import json, boto3\n"
            "class Client:\n"
            "    def get_secret_value(self, **kwargs):\n"
            f"        if {fail!r}:\n"
            "            raise RuntimeError('upstream failure containing sensitive-value')\n"
            f"        return {{'SecretString': {secret_string!r}}}\n"
            "boto3.client = lambda *args, **kwargs: Client()\n"
            "import config.settings as s\n"
            "print(s.SECRET_KEY)\n"
            "print(repr(s.PRR_SETTINGS_ENCRYPTION_KEY))\n"
            "import os\n"
            "print('DJANGO_SECRET_KEY' in os.environ)\n"
            "from django.conf import settings as dj\n"
            "dj.configure(**{k: getattr(s, k) for k in dir(s) if k.isupper()})\n"
            "print(any(isinstance(getattr(dj, k), dict) and 'DJANGO_SECRET_KEY' in getattr(dj, k) for k in dir(dj) if k.isupper()))\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=settings.BASE_DIR,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHON_DOTENV_DISABLED": "1",
                "DJANGO_DEBUG": "true",
                "PRR_RUNTIME_SECRET_ID": SECRET_ID,
                "AWS_DEFAULT_REGION": "us-west-2",
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_settings_take_both_keys_from_the_runtime_secret(self):
        result = self._import_settings(_runtime_secret())

        self.assertEqual(result.returncode, 0, result.stderr)
        # The last line: the secret never entered the process environment.
        # Nor did the loaded dict become a Django setting of its own.
        self.assertEqual(
            result.stdout.splitlines(), [SIGNING_KEY, "''", "False", "False"]
        )

    def test_settings_import_fails_closed_without_leaking(self):
        result = self._import_settings(fail=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertNotIn("sensitive-value", result.stderr)
