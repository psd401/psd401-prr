"""The e2e-only filesystem object store (issue #40).

It stands in for the private bucket only in browser acceptance runs: the
setting is honoured only while the test-fixture switch is on, it never
overwrites an object, and no key may escape its root.
"""

import io
import shutil
import tempfile

from botocore.exceptions import ClientError
from django.test import TestCase, override_settings

from apps.secure_files import _FilesystemObjectStore, _object_store_client


class FilesystemObjectStoreTests(TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="prr-fs-store-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = _FilesystemObjectStore(self.root)

    def test_it_is_used_only_with_the_fixture_switch_on(self):
        with override_settings(PRR_E2E_FILESYSTEM_OBJECT_STORE=self.root, PRR_TEST_FIXTURE_SEED_ENABLED=True):
            self.assertIsInstance(_object_store_client(), _FilesystemObjectStore)
        with override_settings(
            PRR_E2E_FILESYSTEM_OBJECT_STORE=self.root,
            PRR_TEST_FIXTURE_SEED_ENABLED=False,
            OBJECT_STORE={"endpoint": "", "bucket": "b", "access_key": "", "secret_key": "", "region": ""},
        ):
            self.assertNotIsInstance(_object_store_client(), _FilesystemObjectStore)

    def test_put_get_copy_head_delete_and_no_overwrite(self):
        self.store.put_object(Bucket="b", Key="records/a", Body=io.BytesIO(b"abc"), IfNoneMatch="*")
        self.assertEqual(self.store.get_object(Bucket="b", Key="records/a")["Body"].read(), b"abc")
        with self.assertRaises(ClientError):
            self.store.put_object(Bucket="b", Key="records/a", Body=io.BytesIO(b"x"), IfNoneMatch="*")
        self.store.copy_object(Bucket="b", Key="records/b", CopySource={"Bucket": "b", "Key": "records/a"})
        self.assertEqual(self.store.head_object(Bucket="b", Key="records/b")["ContentLength"], 3)
        self.store.delete_object(Bucket="b", Key="records/b")
        with self.assertRaises(ClientError):
            self.store.head_object(Bucket="b", Key="records/b")
        self.assertEqual(self.store.get_bucket_versioning(Bucket="b"), {})

    def test_a_key_may_not_escape_the_root(self):
        with self.assertRaises(ValueError):
            self.store.put_object(Bucket="b", Key="../../escape", Body=io.BytesIO(b"x"))
