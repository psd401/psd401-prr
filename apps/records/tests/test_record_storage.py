"""Integrity and naming contracts for retained records object storage."""

import hashlib
import io

from django.test import TestCase, override_settings

from apps.records.storage import (
    ObjectIntegrityError,
    generated_record_storage_key,
    load_verified_object,
    store_immutable_object,
)


class _Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk

    def close(self):
        super().close()


class _ObjectClient:
    def __init__(self, payload=b"", metadata=None):
        self.payload = payload
        self.metadata = metadata or {}
        self.put_calls = []

    def put_object(self, **kwargs):
        body = kwargs["Body"]
        position = body.tell()
        self.put_calls.append({**kwargs, "Body": body.read()})
        body.seek(position)

    def get_object(self, **kwargs):
        return {
            "Body": _Body(self.payload),
            "ContentLength": len(self.payload),
            "Metadata": self.metadata,
        }


class _OverlongBody:
    def __init__(self):
        self.closed = False

    def iter_chunks(self, chunk_size):
        yield b"too long"
        raise AssertionError("integrity reader continued after exceeding expected size")

    def close(self):
        self.closed = True


class _OverlongObjectClient:
    def __init__(self):
        self.body = _OverlongBody()

    def get_object(self, **kwargs):
        return {"Body": self.body}


@override_settings(OBJECT_STORE={"bucket": "records"})
# TestCase, not SimpleTestCase: the digest-verification memory ceiling is an
# application-managed setting now, so a verified read consults the store.
class RecordObjectStorageTests(TestCase):
    def test_generated_key_never_contains_the_requester_filename(self):
        key = generated_record_storage_key(
            "5f38b254-172d-4262-8489-e49896f41d0d",
            original_filename="../../Student Medical Records.PDF",
        )

        self.assertRegex(
            key,
            r"^records/5f38b254-172d-4262-8489-e49896f41d0d/[0-9a-f-]+\.pdf$",
        )
        self.assertNotIn("student", key.lower())
        self.assertNotIn("..", key)

    def test_immutable_write_rechecks_bytes_and_sets_integrity_metadata(self):
        payload = b"retained original bytes"
        digest = hashlib.sha256(payload).hexdigest()
        source = io.BytesIO(payload)
        source.seek(3)
        client = _ObjectClient()

        store_immutable_object(
            "records/request-id/generated.pdf",
            source,
            content_length=len(payload),
            sha256=digest,
            content_type="application/pdf",
            _client=client,
        )

        self.assertEqual(source.tell(), 3)
        self.assertEqual(len(client.put_calls), 1)
        call = client.put_calls[0]
        self.assertEqual(call["Bucket"], "records")
        self.assertEqual(call["Key"], "records/request-id/generated.pdf")
        self.assertEqual(call["Body"], payload)
        self.assertEqual(call["ContentLength"], len(payload))
        self.assertEqual(call["ContentType"], "application/pdf")
        self.assertEqual(call["Metadata"]["sha256"], digest)
        self.assertEqual(call["Metadata"]["immutable"], "true")
        self.assertEqual(call["IfNoneMatch"], "*")

    def test_immutable_write_rejects_changed_source_before_upload(self):
        client = _ObjectClient()

        with self.assertRaisesMessage(
            ObjectIntegrityError,
            "source bytes do not match",
        ):
            store_immutable_object(
                "records/request-id/generated.pdf",
                io.BytesIO(b"changed"),
                content_length=7,
                sha256="0" * 64,
                content_type="application/pdf",
                _client=client,
            )

        self.assertEqual(client.put_calls, [])

    def test_verified_read_checks_body_instead_of_trusting_object_metadata(self):
        expected = b"expected"
        client = _ObjectClient(
            payload=b"tampered",
            metadata={"sha256": hashlib.sha256(expected).hexdigest()},
        )

        with self.assertRaisesMessage(ObjectIntegrityError, "failed integrity"):
            load_verified_object(
                "records/request-id/generated.pdf",
                expected_size=len(expected),
                expected_sha256=hashlib.sha256(expected).hexdigest(),
                _client=client,
            )

    def test_verified_read_returns_a_rewound_spooled_file(self):
        payload = b"verified records"
        digest = hashlib.sha256(payload).hexdigest()
        client = _ObjectClient(payload=payload, metadata={"sha256": digest})

        verified = load_verified_object(
            "records/request-id/generated.pdf",
            expected_size=len(payload),
            expected_sha256=digest,
            _client=client,
        )
        try:
            self.assertEqual(verified.tell(), 0)
            self.assertEqual(verified.read(), payload)
        finally:
            verified.close()

    def test_verified_read_stops_immediately_when_body_exceeds_expected_size(self):
        client = _OverlongObjectClient()

        with self.assertRaisesMessage(ObjectIntegrityError, "failed integrity"):
            load_verified_object(
                "records/request-id/generated.pdf",
                expected_size=1,
                expected_sha256=hashlib.sha256(b"x").hexdigest(),
                _client=client,
            )

        self.assertTrue(client.body.closed)

    def test_non_record_or_production_key_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "generated records key"):
            store_immutable_object(
                "quarantine/intake/do-not-promote",
                io.BytesIO(b"bytes"),
                content_length=5,
                sha256=hashlib.sha256(b"bytes").hexdigest(),
                content_type="application/octet-stream",
                _client=_ObjectClient(),
            )
