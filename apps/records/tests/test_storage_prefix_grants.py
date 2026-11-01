"""Every storage prefix the application uses must be granted in the CDK role.

This exists because the opposite happened. `quarantine/legacy-files/` and
`derivatives/` were registered in the application and shipped without being
added to the runtime role's S3 policy. Nothing failed in any test suite: the
tests stub the object store, so the gap only appears in production, at the
moment a migration writes its first quarantined file or a reviewer asks for a
rendition of a Word document.

A prefix is a two-sided contract. Asserting one side against the other is the
only place that mismatch is visible before a deployment.
"""
import re
from pathlib import Path

from django.test import TestCase

from apps import secure_files
from apps.records import storage

CDK_STACK = (
    Path(__file__).resolve().parents[3] / "infra/cdk/lib/prr-hosting-stack.ts"
)


def _granted_prefixes() -> set[str]:
    """Prefixes the runtime role may read or write, read out of the stack."""
    source = CDK_STACK.read_text()
    block = re.search(
        r"const readableWritablePrefixes = \[(.*?)\];", source, re.DOTALL
    )
    assert block, "readableWritablePrefixes not found in the CDK stack"
    quarantine = re.search(
        r"const quarantinePrefixes = \[(.*?)\];", source, re.DOTALL
    )
    assert quarantine, "quarantinePrefixes not found in the CDK stack"
    found = set(re.findall(r'"([^"]+)"', block.group(1)))
    found |= set(re.findall(r'"([^"]+)"', quarantine.group(1)))
    return {value.removesuffix("*") for value in found}


class StoragePrefixGrantTests(TestCase):
    def test_every_retained_object_prefix_is_granted(self):
        granted = _granted_prefixes()
        for prefix in storage._ALLOWED_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(
                    prefix,
                    granted,
                    f"{prefix!r} is a generated storage prefix but the runtime "
                    "role cannot write it. Add it to readableWritablePrefixes "
                    "in infra/cdk/lib/prr-hosting-stack.ts.",
                )

    def test_every_quarantine_prefix_is_granted(self):
        granted = _granted_prefixes()
        for prefix in secure_files._QUARANTINE_KEY_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(
                    prefix,
                    granted,
                    f"{prefix!r} is a quarantine prefix but the runtime role "
                    "cannot write or delete it. Add it to quarantinePrefixes "
                    "in infra/cdk/lib/prr-hosting-stack.ts.",
                )
