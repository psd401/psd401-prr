# ABOUTME: Red tests defining the bounded, side-effect-free inbound MIME parser.
# ABOUTME: Raw evidence is hostile; parsing never fetches URLs or trusts MIME labels.
import base64
import hashlib
import importlib
import io
import socket
import struct
import tracemalloc
import urllib.request
import zipfile
from email.message import EmailMessage
from email.policy import SMTP
from unittest.mock import patch

from django.test import SimpleTestCase


def _mime_module():
    return importlib.import_module("apps.correspondence.mime")


def _limits(**overrides):
    mime = _mime_module()
    defaults = {
        "max_raw_bytes": 1024 * 1024,
        "max_header_count": 100,
        "max_header_line_bytes": 998,
        "max_parts": 50,
        "max_depth": 10,
        "max_body_bytes": 256 * 1024,
        "max_attachment_count": 10,
        "max_attachment_bytes": 256 * 1024,
        "max_total_attachment_bytes": 512 * 1024,
    }
    defaults.update(overrides)
    return mime.InboundMimeLimits(**defaults)


def _parse(raw, **limit_overrides):
    mime = _mime_module()
    return mime.parse_inbound_message(raw, limits=_limits(**limit_overrides))


def _message_with_attachment(
    payload,
    *,
    filename="evidence.pdf",
    maintype="application",
    subtype="pdf",
    cte="base64",
):
    message = EmailMessage()
    message["From"] = "requester@example.org"
    message["To"] = "records@example.org"
    message["Subject"] = "PRR response"
    message.set_content("Please see the attached evidence.")
    message.add_attachment(
        payload,
        maintype=maintype,
        subtype=subtype,
        filename=filename,
        cte=cte,
    )
    return message.as_bytes(policy=SMTP)


def _ooxml_payload(content_types, entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class InboundMimeBoundsTests(SimpleTestCase):
    def assert_security_error(self, code, callable_):
        mime = _mime_module()
        with self.assertRaises(mime.InboundMimeSecurityError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn("requester@example.org", str(caught.exception))

    def test_raw_message_limit_is_enforced_before_mime_parsing(self):
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: bounded\r\n\r\n"
            b"body"
        )

        self.assert_security_error(
            "message_too_large",
            lambda: _parse(raw, max_raw_bytes=len(raw) - 1),
        )

    def test_strict_parser_quarantines_structural_and_transfer_encoding_defects(self):
        missing_boundary = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: malformed\r\n"
            b"Content-Type: multipart/mixed; boundary=expected\r\n\r\n"
            b"--different\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        )
        invalid_base64 = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: malformed transfer\r\n"
            b"Content-Type: application/pdf\r\n"
            b"Content-Disposition: attachment; filename=evidence.pdf\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"not-valid-@@@@-base64\r\n"
        )
        invalid_mime_header = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: malformed MIME header\r\n"
            b"Content-Type: text/\r\n\r\n"
            b"body\r\n"
        )
        duplicate_identity_header = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Message-ID: <first@example.org>\r\n"
            b"Message-ID: <second@example.org>\r\n\r\n"
            b"body\r\n"
        )
        invalid_quoted_printable = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            b"not-valid=ZZ\r\n"
        )
        invalid_seven_bit_body = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: 7bit\r\n\r\n"
            b"not-seven-bit:\xff\r\n"
        )

        for raw in (
            missing_boundary,
            invalid_base64,
            invalid_mime_header,
            duplicate_identity_header,
            invalid_quoted_printable,
            invalid_seven_bit_body,
        ):
            with self.subTest(raw=raw[:80]):
                self.assert_security_error(
                    "malformed_message",
                lambda raw=raw: _parse(raw),
            )

    def test_undecodable_encoded_words_are_kept_as_literal_text(self):
        # RFC 2047 section 6.3: an encoded-word that cannot be decoded is shown
        # as ordinary text. It is never a reason to quarantine the message.
        for subject in (
            b"=?utf-8?b?@@@@?=",
            b"Alert =?x-unknown-charset?q?fog?= advisory",
            b"Dense fog =? advisory",
            b"Fog =?utf-8?q?bad=Z?= advisory",
        ):
            with self.subTest(subject=subject):
                raw = (
                    b"From: Pierce County Alert <noreply@example.net>\r\n"
                    b"To: records@example.org\r\n"
                    b"Subject: " + subject + b"\r\n\r\n"
                    b"Fog advisory body\r\n"
                )

                parsed = _parse(raw)

                self.assertIn("Fog advisory body", parsed.body)

    def test_structured_header_parser_failures_are_quarantined(self):
        nested_comment = ("(" * 500) + "requester" + (")" * 500)
        nested_comment_bytes = nested_comment.encode("ascii")
        folded_comment = (
            b"From: "
            + nested_comment_bytes[:900]
            + b"\r\n "
            + nested_comment_bytes[900:]
            + b" <requester@example.org>\r\n"
            b"To: records@example.org\r\n\r\n"
            b"body\r\n"
        )
        malformed_address_index = (
            b"From: , :\r\n"
            b"To: records@example.org\r\n\r\n"
            b"body\r\n"
        )
        malformed_address_attribute = (
            b"From: :;@\r\n"
            b"To: records@example.org\r\n\r\n"
            b"body\r\n"
        )
        malformed_disposition = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Disposition: ;0*\r\n\r\n"
            b"body\r\n"
        )

        for raw in (
            folded_comment,
            malformed_address_index,
            malformed_address_attribute,
            malformed_disposition,
        ):
            with self.subTest(raw=raw[:80]):
                self.assert_security_error(
                    "malformed_message",
                    lambda raw=raw: _parse(raw),
                )

    def test_database_incompatible_nul_text_and_identifiers_are_quarantined(self):
        nul_body = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Message-ID: <safe@example.org>\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"hello\x00world\r\n"
        )
        nul_message_id = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Message-ID: <unsafe\x00identifier@example.org>\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"body\r\n"
        )

        for raw in (nul_body, nul_message_id):
            with self.subTest(raw=raw):
                self.assert_security_error(
                    "malformed_message",
                    lambda raw=raw: _parse(raw),
                )

    def test_header_count_and_physical_line_length_are_bounded(self):
        too_many_headers = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"X-One: 1\r\n"
            b"X-Two: 2\r\n"
            b"X-Three: 3\r\n\r\nbody"
        )
        long_header = (
            b"From: requester@example.org\r\n"
            b"Subject: " + (b"x" * 80) + b"\r\n\r\nbody"
        )

        self.assert_security_error(
            "too_many_headers",
            lambda: _parse(too_many_headers, max_header_count=4),
        )
        self.assert_security_error(
            "header_line_too_long",
            lambda: _parse(long_header, max_header_line_bytes=40),
        )

        distributed = EmailMessage()
        distributed["From"] = "requester@example.org"
        distributed["To"] = "records@example.org"
        distributed["Subject"] = "distributed headers"
        distributed.make_mixed()
        child = EmailMessage()
        child["X-Part-Evidence"] = "one"
        child.set_content("body")
        distributed.attach(child)
        self.assert_security_error(
            "too_many_headers",
            lambda: _parse(
                distributed.as_bytes(policy=SMTP),
                max_header_count=7,
            ),
        )

    def test_non_rfc_control_bytes_do_not_split_physical_header_lines(self):
        for control in (b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e"):
            raw = (
                b"From: requester@example.org\r\n"
                b"To: records@example.org\r\n"
                b"X-Ignored: "
                + (b"a" * 24)
                + control
                + (b"b" * 24)
                + b"\r\n\r\nbody"
            )
            with self.subTest(control=control):
                mime = _mime_module()
                parsed = mime.BytesParser(policy=mime.policy.default).parsebytes(
                    raw
                )
                self.assert_security_error(
                    "header_line_too_long",
                    lambda mime=mime, parsed=parsed: mime._validate_part_headers(
                        parsed,
                        _limits(max_header_line_bytes=40),
                    ),
                )
                self.assert_security_error(
                    "header_line_too_long",
                    lambda raw=raw: _parse(
                        raw,
                        max_header_line_bytes=40,
                    ),
                )

    def test_part_count_is_bounded(self):
        message = EmailMessage()
        message["From"] = "requester@example.org"
        message["To"] = "records@example.org"
        message["Subject"] = "many parts"
        message.make_mixed()
        for index in range(4):
            child = EmailMessage()
            child.set_content(f"part {index}")
            message.attach(child)

        self.assert_security_error(
            "too_many_mime_parts",
            lambda: _parse(message.as_bytes(policy=SMTP), max_parts=3),
        )

        nested = EmailMessage()
        nested["From"] = "forwarder@example.org"
        nested["To"] = "records@example.org"
        nested.set_content("attached message body")
        outer = EmailMessage()
        outer["From"] = "requester@example.org"
        outer["To"] = "records@example.org"
        outer["Subject"] = "attached message counts toward bounds"
        outer.set_content("outer body")
        outer.add_attachment(nested)
        self.assert_security_error(
            "too_many_mime_parts",
            lambda: _parse(outer.as_bytes(policy=SMTP), max_parts=3),
        )

    def test_multipart_nesting_depth_is_bounded(self):
        nested = EmailMessage()
        nested.set_content("innermost")
        for _ in range(5):
            outer = EmailMessage()
            outer.make_mixed()
            outer.attach(nested)
            nested = outer
        nested["From"] = "requester@example.org"
        nested["To"] = "records@example.org"
        nested["Subject"] = "deep nesting"

        self.assert_security_error(
            "mime_depth_exceeded",
            lambda: _parse(nested.as_bytes(policy=SMTP), max_depth=3),
        )

    def test_obvious_raw_part_depth_and_header_excess_is_rejected_before_parsing(self):
        mime = _mime_module()
        too_many_parts = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: multipart/mixed; boundary=parts\r\n\r\n"
            b"--parts\r\nContent-Type: text/plain\r\n\r\none\r\n"
            b"--parts\r\nContent-Type: text/plain\r\n\r\ntwo\r\n"
            b"--parts--\r\n"
        )

        nested = EmailMessage()
        nested.set_content("innermost")
        for _ in range(3):
            outer = EmailMessage()
            outer.make_mixed()
            outer.attach(nested)
            nested = outer
        nested["From"] = "requester@example.org"
        nested["To"] = "records@example.org"

        distributed_headers = EmailMessage()
        distributed_headers["From"] = "requester@example.org"
        distributed_headers["To"] = "records@example.org"
        distributed_headers.make_mixed()
        child = EmailMessage()
        child["X-One"] = "one"
        child["X-Two"] = "two"
        child.set_content("body")
        distributed_headers.attach(child)

        excessive_header_folding = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: folded\r\n"
            + (b"\tcontinuation-value\r\n" * 12)
            + b"\r\nbody\r\n"
        )

        cases = (
            ("too_many_mime_parts", too_many_parts, {"max_parts": 2}),
            (
                "mime_depth_exceeded",
                nested.as_bytes(policy=SMTP),
                {"max_depth": 1},
            ),
            (
                "too_many_headers",
                distributed_headers.as_bytes(policy=SMTP),
                {"max_header_count": 5},
            ),
            (
                "too_many_headers",
                excessive_header_folding,
                {
                    "max_header_count": 5,
                    "max_header_line_bytes": 40,
                },
            ),
        )
        for code, raw, overrides in cases:
            with self.subTest(code=code):
                with patch.object(
                    mime.BytesParser,
                    "parsebytes",
                    side_effect=AssertionError("full MIME parser was invoked"),
                ):
                    self.assert_security_error(
                        code,
                        lambda raw=raw, overrides=overrides: _parse(
                            raw,
                            **overrides,
                        ),
                    )

    def test_bare_cr_multipart_part_bomb_is_rejected_before_parsing(self):
        mime = _mime_module()
        raw = (
            b"From: requester@example.org\r"
            b"To: records@example.org\r"
            b"Content-Type: multipart/mixed; boundary=parts\r\r"
            b"--parts\rContent-Type: text/plain\r\rone\r"
            b"--parts\rContent-Type: text/plain\r\rtwo\r"
            b"--parts\rContent-Type: text/plain\r\rthree\r"
            b"--parts\rContent-Type: text/plain\r\rfour\r"
            b"--parts--\r"
        )

        with patch.object(
            mime.BytesParser,
            "parsebytes",
            side_effect=AssertionError("full MIME parser was invoked"),
        ):
            self.assert_security_error(
                "too_many_mime_parts",
                lambda: _parse(raw, max_parts=3),
            )

    def test_delivery_status_record_bomb_is_rejected_before_parsing(self):
        mime = _mime_module()
        raw = (
            b"From: mailer-daemon@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: message/delivery-status\r\n\r\n"
            + (b"X-Status-Field: bounded\r\n\r\n" * 8)
        )

        with patch.object(
            mime.BytesParser,
            "parsebytes",
            side_effect=AssertionError("full MIME parser was invoked"),
        ):
            self.assert_security_error(
                "too_many_mime_parts",
                lambda: _parse(raw, max_parts=5),
            )

    def test_unsupported_message_subtype_is_rejected_before_parsing(self):
        mime = _mime_module()
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: message/x-hostile\r\n\r\n"
            b"Content-Type: multipart/mixed; boundary=parts\r\n\r\n"
            b"--parts\r\nContent-Type: text/plain\r\n\r\none\r\n"
            b"--parts\r\nContent-Type: text/plain\r\n\r\ntwo\r\n"
            b"--parts\r\nContent-Type: text/plain\r\n\r\nthree\r\n"
            b"--parts--\r\n"
        )

        with patch.object(
            mime.BytesParser,
            "parsebytes",
            side_effect=AssertionError("full MIME parser was invoked"),
        ):
            self.assert_security_error(
                "malformed_message",
                lambda: _parse(raw),
            )

    def test_encoded_rfc822_literal_part_bomb_is_rejected_before_parsing(self):
        mime = _mime_module()
        for transfer_encoding in (b"base64", b"quoted-printable"):
            with self.subTest(transfer_encoding=transfer_encoding):
                raw = (
                    b"From: requester@example.org\r\n"
                    b"To: records@example.org\r\n"
                    b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
                    b"--outer\r\n"
                    b"Content-Type: message/rfc822\r\n"
                    b"Content-Disposition: attachment; filename=forwarded.eml\r\n"
                    b"Content-Transfer-Encoding: "
                    + transfer_encoding
                    + b"\r\n\r\n"
                    b"Content-Type: multipart/mixed; boundary=inner\r\n\r\n"
                    + b"".join(
                        (
                            b"--inner\r\n"
                            b"Content-Type: text/plain\r\n\r\n"
                            b"nested\r\n"
                        )
                        for _index in range(8)
                    )
                    + b"--inner--\r\n"
                    b"--outer--\r\n"
                )

                with patch.object(
                    mime.BytesParser,
                    "parsebytes",
                    side_effect=AssertionError(
                        "full MIME parser was invoked"
                    ),
                ):
                    self.assert_security_error(
                        "too_many_mime_parts",
                        lambda raw=raw: _parse(raw, max_parts=5),
                    )

    def test_encoded_rfc822_noncanonical_header_prefix_is_rejected_preparse(
        self,
    ):
        mime = _mime_module()
        prefixes = (
            b"From attacker Sat Jan  1 00:00:00 2000\r\n",
            b" leading-continuation\r\n",
            b": empty-header-name\r\n",
        )
        for transfer_encoding in (b"base64", b"quoted-printable"):
            for prefix in prefixes:
                with self.subTest(
                    transfer_encoding=transfer_encoding,
                    prefix=prefix,
                ):
                    raw = (
                        b"From: requester@example.org\r\n"
                        b"To: records@example.org\r\n"
                        b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
                        b"--outer\r\n"
                        b"Content-Type: message/rfc822\r\n"
                        b"Content-Disposition: attachment; filename=forwarded.eml\r\n"
                        b"Content-Transfer-Encoding: "
                        + transfer_encoding
                        + b"\r\n\r\n"
                        + prefix
                        + b"Content-Type: multipart/mixed; boundary=inner\r\n\r\n"
                        + b"".join(
                            (
                                b"--inner\r\n"
                                b"Content-Type: text/plain\r\n\r\n"
                                b"nested\r\n"
                            )
                            for _index in range(8)
                        )
                        + b"--inner--\r\n"
                        b"--outer--\r\n"
                    )

                    with patch.object(
                        mime.BytesParser,
                        "parsebytes",
                        side_effect=AssertionError(
                            "full MIME parser was invoked"
                        ),
                    ):
                        self.assert_security_error(
                            "malformed_message",
                            lambda raw=raw: _parse(raw, max_parts=5),
                        )

    def test_quoted_printable_rfc822_may_encode_the_first_header_colon(self):
        expected = (
            b"From: nested@example.org\n"
            b"To: records@example.org\n"
            b"Subject: valid encoded message\n\n"
            b"nested body\n"
        )
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\n"
            b"Content-Type: message/rfc822\r\n"
            b"Content-Disposition: attachment; filename=forwarded.eml\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            b"From=3A nested@example.org\n"
            b"To: records@example.org\n"
            b"Subject: valid encoded message\n\n"
            b"nested body\n"
            b"\r\n--outer--\r\n"
        )

        parsed = _parse(raw)

        [attachment] = parsed.attachments
        self.assertEqual(attachment.content, expected)

    def test_unsupported_multipart_boundaries_are_rejected_before_parsing(self):
        mime = _mime_module()
        boundary_forms = (
            b"boundary*=utf-8''parts",
            b"boundary*0*=us-ascii''par; boundary*1*=ts",
            b"boundary=(comment)parts",
            b"boundary=parts(comment)",
            b"boundary=parts; boundary=parts",
            b"boundary=parts; boundary=other",
            b"boundary*=utf-8''other; boundary=parts",
            b"boundary=" + (b"x" * 71),
            b'boundary="' + (b"x" * 71) + b'"',
        )

        for boundary_form in boundary_forms:
            with self.subTest(boundary_form=boundary_form):
                raw = (
                    b"From: requester@example.org\r\n"
                    b"To: records@example.org\r\n"
                    b"Content-Type: multipart/mixed; "
                    + boundary_form
                    + b"\r\n\r\n"
                    b"--parts\r\nContent-Type: text/plain\r\n\r\none\r\n"
                    b"--parts--\r\n"
                )
                with patch.object(
                    mime.BytesParser,
                    "parsebytes",
                    side_effect=AssertionError("full MIME parser was invoked"),
                ):
                    self.assert_security_error(
                        "malformed_message",
                        lambda raw=raw: _parse(raw),
                    )

    def test_duplicate_multipart_content_type_is_rejected_before_parsing(self):
        mime = _mime_module()
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: multipart/mixed; boundary=parts\r\n"
            b"Content-Type: multipart/mixed; boundary=other\r\n\r\n"
            b"--parts\r\nContent-Type: text/plain\r\n\r\none\r\n"
            b"--parts--\r\n"
        )

        with patch.object(
            mime.BytesParser,
            "parsebytes",
            side_effect=AssertionError("full MIME parser was invoked"),
        ):
            self.assert_security_error(
                "malformed_message",
                lambda: _parse(raw),
            )

    def test_nested_raw_preflight_does_not_copy_large_parent_payloads(self):
        mime = _mime_module()
        raw = (
            b"Content-Type: text/plain; charset=us-ascii\r\n\r\n"
            + (b"x" * (2 * 1024 * 1024))
        )
        for index in range(8):
            boundary = f"bounded-layer-{index}".encode("ascii")
            raw = (
                b"Content-Type: multipart/mixed; boundary="
                + boundary
                + b"\r\n\r\n--"
                + boundary
                + b"\r\n"
                + raw
                + b"\r\n--"
                + boundary
                + b"--\r\n"
            )
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            + raw
        )
        limits = _limits(
            max_raw_bytes=len(raw) + 1,
            max_parts=20,
            max_depth=10,
        )

        tracemalloc.start()
        try:
            mime._preflight_raw_structure(raw, limits)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # The source bytes predate tracing. A peak below half their size proves
        # recursive traversal is retaining spans, not copies of each parent.
        self.assertLess(peak, len(raw) // 2)

    def test_oversized_base64_is_rejected_before_allocating_decoded_payload(self):
        mime = _mime_module()
        attachment = _message_with_attachment(
            b"%PDF-1.7\n" + (b"x" * 64),
        )
        body = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + base64.b64encode(b"x" * 64)
            + b"\r\n"
        )

        for code, raw, overrides in (
            ("attachment_too_large", attachment, {"max_attachment_bytes": 16}),
            ("body_too_large", body, {"max_body_bytes": 16}),
        ):
            with self.subTest(code=code):
                with patch.object(
                    mime.base64,
                    "b64decode",
                    side_effect=AssertionError("whole payload was decoded"),
                ):
                    self.assert_security_error(
                        code,
                        lambda raw=raw, overrides=overrides: _parse(
                            raw,
                            **overrides,
                        ),
                    )

    def test_oversized_quoted_printable_is_rejected_before_decoding_payload(self):
        mime = _mime_module()
        attachment = _message_with_attachment(
            b"plain attachment evidence " * 4,
            filename="evidence.txt",
            maintype="text",
            subtype="plain",
            cte="quoted-printable",
        )
        body = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            + (b"plain-body-evidence=20" * 4)
            + b"\r\n"
        )

        for code, raw, overrides in (
            (
                "attachment_too_large",
                attachment,
                {"max_attachment_bytes": 16},
            ),
            ("body_too_large", body, {"max_body_bytes": 16}),
        ):
            with self.subTest(code=code):
                with patch.object(
                    mime.quopri,
                    "decodestring",
                    side_effect=AssertionError("whole payload was decoded"),
                ):
                    self.assert_security_error(
                        code,
                        lambda raw=raw, overrides=overrides: _parse(
                            raw,
                            **overrides,
                        ),
                    )

    def test_zip_entry_count_is_rejected_from_eocd_before_zip_materialization(self):
        mime = _mime_module()
        local_file_prefix = b"PK\x03\x04" + (b"\x00" * 26)
        eocd = struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            10_001,
            10_001,
            0,
            len(local_file_prefix),
            0,
        )

        with patch.object(
            mime.zipfile,
            "ZipFile",
            side_effect=AssertionError("ZIP directory was materialized"),
        ):
            self.assertIsNone(
                mime._ooxml_content_type(local_file_prefix + eocd)
            )

        central_record = b"PK\x01\x02" + (b"\x00" * 42)
        dishonest_directory = central_record * 2
        dishonest_eocd = struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            1,
            1,
            len(dishonest_directory),
            len(local_file_prefix),
            0,
        )
        with patch.object(
            mime.zipfile,
            "ZipFile",
            side_effect=AssertionError("ZIP directory was materialized"),
        ):
            self.assertIsNone(
                mime._ooxml_content_type(
                    local_file_prefix
                    + dishonest_directory
                    + dishonest_eocd
                )
            )

    def test_decoded_attachment_count_per_file_and_total_bytes_are_bounded(self):
        oversized = _message_with_attachment(b"%PDF-1.7\n" + b"x" * 20)
        self.assert_security_error(
            "attachment_too_large",
            lambda: _parse(oversized, max_attachment_bytes=16),
        )

        message = EmailMessage()
        message["From"] = "requester@example.org"
        message["To"] = "records@example.org"
        message["Subject"] = "combined limit"
        message.set_content("attachments")
        for index in range(3):
            message.add_attachment(
                b"%PDF-" + bytes([48 + index]) * 8,
                maintype="application",
                subtype="pdf",
                filename=f"evidence-{index}.pdf",
                cte="base64",
            )
        raw = message.as_bytes(policy=SMTP)

        self.assert_security_error(
            "too_many_attachments",
            lambda: _parse(raw, max_attachment_count=2),
        )
        self.assert_security_error(
            "attachment_total_too_large",
            lambda: _parse(
                raw,
                max_attachment_count=3,
                max_attachment_bytes=64,
                max_total_attachment_bytes=24,
            ),
        )


class InboundMimeExtractionTests(SimpleTestCase):
    def assert_security_error(self, code, callable_):
        mime = _mime_module()
        with self.assertRaises(mime.InboundMimeSecurityError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)

    def test_encoded_headers_are_decoded_to_bounded_unicode(self):
        raw = (
            b"From: =?utf-8?q?Jos=C3=A9_Requester?= <requester@example.org>\r\n"
            b"To: records@example.org\r\n"
            b"Subject: =?utf-8?b?UsOpc3Vtw6kg4oCTIEp1bHk=?=\r\n"
            b"Message-ID: <inbound-1@example.org>\r\n\r\n"
            b"Plain body.\r\n"
        )

        parsed = _parse(raw)

        self.assertEqual(parsed.subject, "Résumé – July")
        self.assertEqual(parsed.from_address, "requester@example.org")
        self.assertEqual(parsed.rfc_message_id, "<inbound-1@example.org>")

    def test_top_level_recipient_and_allowlisted_header_evidence_is_immutable(self):
        raw = (
            b"From: =?utf-8?q?Jos=C3=A9_Requester?= <requester@example.org>\r\n"
            b"To: Records <records@example.org>, Archive <archive@example.org>\r\n"
            b"Cc: Counsel <counsel@example.org>\r\n"
            b"Subject: =?utf-8?b?UsOpc3Vtw6kg4oCTIEp1bHk=?=\r\n"
            b"Message-ID: <inbound-1@example.org>\r\n"
            b"X-Provider-Secret: must-not-be-persisted\r\n\r\n"
            b"Plain body.\r\n"
        )

        parsed = _parse(raw)

        self.assertEqual(
            parsed.to_addresses,
            ("records@example.org", "archive@example.org"),
        )
        self.assertEqual(parsed.cc_addresses, ("counsel@example.org",))
        self.assertEqual(parsed.headers["Subject"], "Résumé – July")
        self.assertEqual(
            parsed.headers["From"],
            "José Requester <requester@example.org>",
        )
        self.assertNotIn("X-Provider-Secret", parsed.headers)
        with self.assertRaises(TypeError):
            parsed.headers["Subject"] = "changed"

    def test_plain_text_is_preferred_over_html_alternative(self):
        message = EmailMessage()
        message["From"] = "requester@example.org"
        message["To"] = "records@example.org"
        message["Subject"] = "alternative"
        message.set_content("The exact plain-text response.")
        message.add_alternative(
            "<p>Different <strong>HTML</strong> response.</p>",
            subtype="html",
        )

        parsed = _parse(message.as_bytes(policy=SMTP))

        self.assertEqual(parsed.body.strip(), "The exact plain-text response.")
        self.assertNotIn("<", parsed.body)

    def test_blank_plain_text_falls_back_to_sanitized_html_alternative(self):
        message = EmailMessage()
        message["From"] = "requester@example.org"
        message["To"] = "records@example.org"
        message["Subject"] = "blank plain alternative"
        message.set_content(" \t\r\n  ")
        message.add_alternative(
            (
                "<p>Substantive <strong>HTML response</strong>.</p>"
                "<a href='https://tracker.invalid/pixel'>Visible label</a>"
                "<script>stealSecrets()</script>"
            ),
            subtype="html",
        )

        with (
            patch.object(socket, "create_connection") as connect,
            patch.object(urllib.request, "urlopen") as urlopen,
        ):
            parsed = _parse(message.as_bytes(policy=SMTP))

        connect.assert_not_called()
        urlopen.assert_not_called()
        self.assertIn("Substantive HTML response.", parsed.body)
        self.assertIn("Visible label", parsed.body)
        self.assertNotIn("tracker.invalid", parsed.body)
        self.assertNotIn("stealSecrets", parsed.body)
        self.assertNotIn("<", parsed.body)

    def test_related_declared_root_ignores_earlier_inline_text_decoy(self):
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: related declared root\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/related; boundary=\"related\"; "
            b"start=\"<root@example.org>\"\r\n\r\n"
            b"--related\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-ID: <decoy@example.org>\r\n\r\n"
            b"DECOY RELATED TEXT\r\n"
            b"--related\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Content-ID: <root@example.org>\r\n\r\n"
            b"<p>Actual related root.</p>\r\n"
            b"--related--\r\n"
        )

        parsed = _parse(raw)

        self.assertIn("Actual related root.", parsed.body)
        self.assertNotIn("DECOY", parsed.body)

    def test_related_unresolvable_start_falls_back_to_first_part(self):
        # Selecting the related root only chooses which of the sender's own
        # parts is the body; it must never quarantine the whole message.
        cases = (
            (
                "missing",
                b'start="<root@example.org>"',
                (
                    b"--related\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-ID: <other@example.org>\r\n\r\n"
                    b"first part body\r\n"
                ),
            ),
            (
                "duplicate",
                b'start="<root@example.org>"',
                (
                    b"--related\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-ID: <root@example.org>\r\n\r\n"
                    b"first part body\r\n"
                    b"--related\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Content-ID: <root@example.org>\r\n\r\n"
                    b"<p>second</p>\r\n"
                ),
            ),
            (
                "bare-start",
                b'start="root"',
                (
                    b"--related\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-ID: root\r\n\r\n"
                    b"first part body\r\n"
                ),
            ),
            (
                "missing-domain-start",
                b'start="<root>"',
                (
                    b"--related\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-ID: <root>\r\n\r\n"
                    b"first part body\r\n"
                ),
            ),
        )
        for label, start, parts in cases:
            with self.subTest(label=label):
                raw = (
                    b"From: requester@example.org\r\n"
                    b"To: records@example.org\r\n"
                    b"Subject: unresolvable related root\r\n"
                    b"MIME-Version: 1.0\r\n"
                    b"Content-Type: multipart/related; boundary=\"related\"; "
                    + start
                    + b"\r\n\r\n"
                    + parts
                    + b"--related--\r\n"
                )

                parsed = _parse(raw)

                self.assertIn("first part body", parsed.body)

    def test_related_non_rfc_content_ids_do_not_quarantine(self):
        # Gmail's inline images use ``<ii_...>`` and its bounce notices use
        # ``<icon.png>``; neither is an RFC message ID.
        for content_id in (b"<ii_mgs7k2ab0>", b"<icon.png>", b"bare-decoy"):
            with self.subTest(content_id=content_id):
                raw = (
                    b"From: \"Doe, Dana\" <staff@example.org>\r\n"
                    b"To: records@example.org\r\n"
                    b"Subject: Fwd: Data request\r\n"
                    b"MIME-Version: 1.0\r\n"
                    b"Content-Type: multipart/related; "
                    b"boundary=\"related\"\r\n\r\n"
                    b"--related\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                    b"Signed request body\r\n"
                    b"--related\r\n"
                    b"Content-Type: image/png\r\n"
                    b"Content-Disposition: inline; filename=\"image001.png\"\r\n"
                    b"Content-Transfer-Encoding: base64\r\n"
                    b"Content-ID: " + content_id + b"\r\n\r\n"
                    b"iVBORw0KGgo=\r\n"
                    b"--related--\r\n"
                )

                parsed = _parse(raw)

                self.assertIn("Signed request body", parsed.body)
                self.assertEqual(
                    [attachment.filename for attachment in parsed.attachments],
                    ["image001.png"],
                )

    def test_related_type_mismatch_keeps_selected_root(self):
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: conflicting related type\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/related; boundary=\"related\"; "
            b"type=\"text/html\"; start=\"<root@example.org>\"\r\n\r\n"
            b"--related\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-ID: <root@example.org>\r\n\r\n"
            b"body\r\n"
            b"--related--\r\n"
        )

        parsed = _parse(raw)

        self.assertIn("body", parsed.body)

    def test_related_without_start_uses_first_part_as_default_root(self):
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: related default root\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/related; boundary=\"related\"\r\n\r\n"
            b"--related\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n\r\n"
            b"<p>First part is the related root.</p>\r\n"
            b"--related\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"DECOY LATER TEXT\r\n"
            b"--related--\r\n"
        )

        parsed = _parse(raw)

        self.assertIn("First part is the related root.", parsed.body)
        self.assertNotIn("DECOY", parsed.body)

    def test_mixed_uses_first_body_part_and_still_extracts_attachment(self):
        message = EmailMessage()
        message["From"] = "requester@example.org"
        message["To"] = "records@example.org"
        message["Subject"] = "mixed structural body"
        message.set_content("<p>Actual mixed message body.</p>", subtype="html")
        message.make_mixed()

        decoy = EmailMessage()
        decoy.set_content("DECOY LATER INLINE TEXT")
        message.attach(decoy)
        message.add_attachment(
            b"%PDF-1.7\nmixed evidence",
            maintype="application",
            subtype="pdf",
            filename="evidence.pdf",
        )

        parsed = _parse(message.as_bytes(policy=SMTP))

        self.assertEqual(parsed.body, "Actual mixed message body.")
        self.assertNotIn("DECOY", parsed.body)
        [attachment] = parsed.attachments
        self.assertEqual(attachment.filename, "evidence.pdf")
        self.assertEqual(attachment.content, b"%PDF-1.7\nmixed evidence")

    def test_nested_alternative_uses_only_declared_related_root(self):
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: nested related alternative\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/alternative; boundary=\"alternative\"\r\n"
            b"\r\n"
            b"--alternative\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b" \t\r\n"
            b"--alternative\r\n"
            b"Content-Type: multipart/related; boundary=\"related\"; "
            b"start=\"<root@example.org>\"\r\n\r\n"
            b"--related\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-ID: <decoy@example.org>\r\n\r\n"
            b"NESTED DECOY TEXT\r\n"
            b"--related\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Content-ID: <root@example.org>\r\n\r\n"
            b"<p>Nested actual HTML root.</p>\r\n"
            b"--related--\r\n"
            b"--alternative--\r\n"
        )

        parsed = _parse(raw)

        self.assertIn("Nested actual HTML root.", parsed.body)
        self.assertNotIn("DECOY", parsed.body)

    def test_alternative_tries_html_candidates_until_one_is_nonblank(self):
        message = EmailMessage()
        message["From"] = "requester@example.org"
        message["To"] = "records@example.org"
        message["Subject"] = "multiple html alternatives"
        message.set_content(" \t\r\n")
        message.add_alternative(
            "<script>ignoredOnlyContent()</script>",
            subtype="html",
        )
        message.add_alternative(
            "<p>Second substantive HTML alternative.</p>",
            subtype="html",
        )

        parsed = _parse(message.as_bytes(policy=SMTP))

        self.assertEqual(
            parsed.body,
            "Second substantive HTML alternative.",
        )
        self.assertNotIn("ignoredOnlyContent", parsed.body)

    def test_self_closing_ignored_html_tag_suppresses_following_content(self):
        for tag in ("script", "style", "template"):
            with self.subTest(tag=tag):
                message = EmailMessage()
                message["From"] = "requester@example.org"
                message["To"] = "records@example.org"
                message["Subject"] = f"self-closing {tag}"
                message.set_content(
                    (
                        f"<p>Visible before {tag}.</p>"
                        f"<{tag}/>HIDDEN {tag} CONTENT"
                        "<p>HIDDEN TAIL</p>"
                    ),
                    subtype="html",
                )

                parsed = _parse(message.as_bytes(policy=SMTP))

                self.assertIn(f"Visible before {tag}.", parsed.body)
                self.assertNotIn("HIDDEN", parsed.body)

    def test_ignored_tag_requires_its_matching_end_tag(self):
        for opening_tag in ("<script>", "<script/>"):
            with self.subTest(opening_tag=opening_tag):
                message = EmailMessage()
                message["From"] = "requester@example.org"
                message["To"] = "records@example.org"
                message["Subject"] = "mismatched ignored end tag"
                message.set_content(
                    (
                        "<p>Visible before.</p>"
                        f"{opening_tag}HIDDEN BEFORE MISMATCH"
                        "</style>HIDDEN AFTER MISMATCH"
                        "</script><p>Visible after.</p>"
                    ),
                    subtype="html",
                )

                parsed = _parse(message.as_bytes(policy=SMTP))

                self.assertIn("Visible before.", parsed.body)
                self.assertIn("Visible after.", parsed.body)
                self.assertNotIn("HIDDEN", parsed.body)

    def test_html_only_body_becomes_plain_text_without_scripts_or_url_fetching(self):
        message = EmailMessage()
        message["From"] = "requester@example.org"
        message["To"] = "records@example.org"
        message["Subject"] = "html only"
        message.set_content(
            (
                "<html><body><p>Hello requester team.</p>"
                "<a href='https://tracker.invalid/pixel'>Visible label</a>"
                "<img src='https://tracker.invalid/image'>"
                "<script>stealSecrets()</script></body></html>"
            ),
            subtype="html",
        )

        with (
            patch.object(socket, "create_connection") as connect,
            patch.object(urllib.request, "urlopen") as urlopen,
        ):
            parsed = _parse(message.as_bytes(policy=SMTP))

        connect.assert_not_called()
        urlopen.assert_not_called()
        self.assertIn("Hello requester team.", parsed.body)
        self.assertIn("Visible label", parsed.body)
        self.assertNotIn("tracker.invalid", parsed.body)
        self.assertNotIn("stealSecrets", parsed.body)
        self.assertNotIn("<", parsed.body)

    def test_nested_attached_message_headers_never_replace_top_level_match_headers(self):
        nested = EmailMessage()
        nested["From"] = "attacker@example.org"
        nested["To"] = "records@example.org"
        nested["Subject"] = "forwarded content"
        nested["In-Reply-To"] = (
            "<00000000-0000-0000-0000-000000000000."
            "correspondence@prr.psd401.net>"
        )
        nested.set_content("nested")

        outer = EmailMessage()
        outer["From"] = "requester@example.org"
        outer["To"] = "records@example.org"
        outer["Subject"] = "Fwd: unrelated"
        outer.set_content("outer")
        outer.add_attachment(
            nested.as_bytes(policy=SMTP),
            maintype="message",
            subtype="rfc822",
            filename="forwarded.eml",
        )

        parsed = _parse(outer.as_bytes(policy=SMTP))

        self.assertEqual(parsed.in_reply_to, ())
        self.assertEqual(parsed.references, ())

    def test_bare_nested_rfc822_is_attachment_evidence_not_top_level_body(self):
        nested = (
            b"From: nested-attacker@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: nested evidence\r\n\r\n"
            b"nested body must remain attachment evidence\r\n"
        )
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: forwarded evidence\r\n"
            b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\n"
            b"Content-Type: message/rfc822\r\n\r\n"
            + nested
            + b"\r\n--outer--\r\n"
        )

        parsed = _parse(raw)

        self.assertEqual(parsed.body, "")
        [attachment] = parsed.attachments
        self.assertEqual(attachment.content_type, "message/rfc822")
        self.assertIn(b"nested-attacker@example.org", attachment.content)
        self.assertIn(
            b"nested body must remain attachment evidence",
            attachment.content,
        )

    def test_root_rfc822_wrapper_is_body_not_an_extracted_attachment(self):
        nested = (
            b"From: nested@example.org\n"
            b"To: records@example.org\n"
            b"Subject: wrapped\n\n"
            b"wrapped body\n"
        )
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: root wrapper\r\n"
            b"Content-Type: message/rfc822\r\n\r\n"
            + nested
        )

        parsed = _parse(raw)

        self.assertEqual(parsed.body, "wrapped body\n")
        self.assertEqual(parsed.attachments, ())

        root_attachment = raw.replace(
            b"Content-Type: message/rfc822\r\n",
            (
                b"Content-Type: message/rfc822\r\n"
                b"Content-Disposition: attachment; filename=wrapper.eml\r\n"
            ),
        )
        self.assert_security_error(
            "malformed_message",
            lambda: _parse(root_attachment),
        )

        encoded_root = raw.replace(
            b"Content-Type: message/rfc822\r\n\r\n" + nested,
            (
                b"Content-Type: message/rfc822\r\n"
                b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
                b"From:=20nested@example.org\n"
                b"To:=20records@example.org\n"
                b"Subject:=20wrapped\n\n"
                b"wrapped=20body\n"
            ),
        )
        self.assert_security_error(
            "malformed_message",
            lambda: _parse(encoded_root),
        )

    def test_implicit_multipart_digest_message_preserves_exact_bytes(self):
        nested = (
            b"From: nested@example.org\n"
            b"To: records@example.org\n"
            b"Subject: digest child\n\n"
            b"digest body\n"
        )
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: digest\r\n"
            b"Content-Type: multipart/digest; boundary=digest\r\n\r\n"
            b"--digest\r\n"
            b"\r\n"
            + nested
            + b"\r\n--digest--\r\n"
        )

        parsed = _parse(raw)

        self.assertEqual(parsed.body, "")
        [attachment] = parsed.attachments
        self.assertEqual(attachment.content_type, "message/rfc822")
        self.assertEqual(attachment.content, nested)

    def test_attached_rfc822_preserves_exact_transfer_decoded_bytes(self):
        nested_messages = {
            "identity": (
                b"From: nested@example.org\n"
                b"To: records@example.org\n"
                b"Subject: identity\n\n"
                b"identity body without CRLF normalization\n"
            ),
            "7bit": (
                b"From: nested@example.org\n"
                b"To: records@example.org\n"
                b"Subject: seven bit\n\n"
                b"seven bit body\n"
            ),
            "8bit": (
                b"From: nested@example.org\n"
                b"To: records@example.org\n"
                b"Subject: eight bit\n\n"
                b"eight bit caf\xc3\xa9\n"
            ),
            "binary": (
                b"From: nested@example.org\r\n"
                b"To: records@example.org\r\n"
                b"Subject: binary\r\n\r\n"
                b"binary caf\xc3\xa9 without final newline"
            ),
            "quoted-printable": (
                b"From: nested@example.org\n"
                b"To: records@example.org\n"
                b"Subject: quoted printable\n\n"
                b"quoted printable body = exact\n"
            ),
            "base64": (
                b"From: nested@example.org\n"
                b"To: records@example.org\n"
                b"Subject: base64\n\n"
                b"base64 body\n"
            ),
        }
        encoded_payloads = {
            "identity": nested_messages["identity"],
            "7bit": nested_messages["7bit"],
            "8bit": nested_messages["8bit"],
            "binary": nested_messages["binary"],
            "quoted-printable": (
                b"From:=20nested@example.org\n"
                b"To:=20records@example.org\n"
                b"Subject:=20quoted=20printable\n\n"
                b"quoted=20printable=20body=20=3D=20exact\n"
            ),
            "base64": base64.b64encode(nested_messages["base64"]),
        }
        transfer_headers = {
            "identity": b"",
            "7bit": b"Content-Transfer-Encoding: 7bit\r\n",
            "8bit": b"Content-Transfer-Encoding: 8bit\r\n",
            "binary": b"Content-Transfer-Encoding: binary\r\n",
            "quoted-printable": (
                b"Content-Transfer-Encoding: quoted-printable\r\n"
            ),
            "base64": b"Content-Transfer-Encoding: base64\r\n",
        }

        for encoding, expected in nested_messages.items():
            with self.subTest(encoding=encoding):
                raw = (
                    b"From: requester@example.org\r\n"
                    b"To: records@example.org\r\n"
                    b"Subject: exact attached message\r\n"
                    b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
                    b"--outer\r\n"
                    b"Content-Type: message/rfc822\r\n"
                    b"Content-Disposition: attachment; filename=forwarded.eml\r\n"
                    + transfer_headers[encoding]
                    + b"\r\n"
                    + encoded_payloads[encoding]
                    + b"\r\n--outer--\r\n"
                )

                parsed = _parse(raw)

                [attachment] = parsed.attachments
                self.assertEqual(attachment.content, expected)
                self.assertEqual(attachment.size, len(expected))
                self.assertEqual(
                    attachment.sha256,
                    hashlib.sha256(expected).hexdigest(),
                )

    def test_attachment_filename_controls_and_paths_are_removed(self):
        payload = b"%PDF-1.7\nsafe"
        raw = (
            b"From: requester@example.org\r\n"
            b"To: records@example.org\r\n"
            b"Subject: filename\r\n"
            b"Content-Type: multipart/mixed; boundary=parts\r\n\r\n"
            b"--parts\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            b"--parts\r\nContent-Type: application/pdf\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b"Content-Disposition: attachment;\r\n"
            b" filename*=utf-8''..%2Ffolder%2Fbad%0D%0Aname.pdf\r\n\r\n"
            + base64.b64encode(payload)
            + b"\r\n--parts--\r\n"
        )

        parsed = _parse(raw)

        [attachment] = parsed.attachments
        self.assertEqual(attachment.filename, "badname.pdf")
        self.assertNotIn("/", attachment.filename)
        self.assertFalse(
            any(ord(character) < 32 for character in attachment.filename)
        )

        for label, encoded_path in (
            (
                "fullwidth-slash",
                b"..%EF%BC%8Ffolder%EF%BC%8Fnormalized.pdf",
            ),
            (
                "fullwidth-backslash",
                b"..%EF%BC%BCfolder%EF%BC%BCnormalized.pdf",
            ),
        ):
            with self.subTest(label=label):
                unicode_path = raw.replace(
                    b"..%2Ffolder%2Fbad%0D%0Aname.pdf",
                    encoded_path,
                )
                [normalized] = _parse(unicode_path).attachments
                self.assertEqual(normalized.filename, "normalized.pdf")
                self.assertNotIn("/", normalized.filename)
                self.assertNotIn("\\", normalized.filename)

        dot_filename = raw.replace(
            b"..%2Ffolder%2Fbad%0D%0Aname.pdf",
            b"..",
        )
        [normalized_dot] = _parse(dot_filename).attachments
        self.assertEqual(normalized_dot.filename, "attachment")

    def test_content_type_is_server_sniffed_and_spoofed_labels_are_rejected(self):
        payload = b"%PDF-1.7\nserver-sniffed"
        parsed = _parse(_message_with_attachment(payload))

        [attachment] = parsed.attachments
        self.assertEqual(attachment.content_type, "application/pdf")
        self.assertEqual(attachment.size, len(payload))
        self.assertEqual(
            attachment.sha256,
            hashlib.sha256(payload).hexdigest(),
        )

        spoofed = _message_with_attachment(
            payload,
            maintype="image",
            subtype="png",
            filename="disguised.png",
        )
        self.assert_security_error(
            "attachment_content_type_mismatch",
            lambda: _parse(spoofed),
        )

    def test_ambiguous_ole_and_text_types_are_stable_and_server_owned(self):
        ole_payload = (
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
            + (b"\x00" * 64)
        )
        text_payload = b"request_number,detail\r\nPRR-2026-0001,Same bytes\r\n"

        ole_types = {
            _parse(
                _message_with_attachment(
                    ole_payload,
                    filename="legacy.doc",
                    maintype="application",
                    subtype="msword",
                )
            ).attachments[0].content_type,
            _parse(
                _message_with_attachment(
                    ole_payload,
                    filename="legacy.bin",
                    maintype="application",
                    subtype="octet-stream",
                )
            ).attachments[0].content_type,
        }
        text_types = {
            _parse(
                _message_with_attachment(
                    text_payload,
                    filename="data.csv",
                    maintype="text",
                    subtype="csv",
                )
            ).attachments[0].content_type,
            _parse(
                _message_with_attachment(
                    text_payload,
                    filename="data.txt",
                    maintype="text",
                    subtype="plain",
                )
            ).attachments[0].content_type,
        }

        self.assertEqual(ole_types, {"application/octet-stream"})
        self.assertEqual(text_types, {"text/plain"})

    def test_ooxml_type_comes_from_content_types_manifest_and_main_part(self):
        word_manifest = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/"
            b"content-types'>"
            b"<Override PartName='/word/document.xml' "
            b"ContentType='application/vnd.openxmlformats-officedocument."
            b"wordprocessingml.document.main+xml'/>"
            b"</Types>"
        )
        valid = _ooxml_payload(
            word_manifest,
            {"word/document.xml": b"<document/>"},
        )
        prefix_only = _ooxml_payload(
            (
                b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/"
                b"content-types'><Default Extension='xml' "
                b"ContentType='application/xml'/></Types>"
            ),
            {"word/document.xml": b"<document/>"},
        )
        manifest_without_main_part = _ooxml_payload(
            word_manifest,
            {"word/decoy.xml": b"<document/>"},
        )

        parsed = _parse(
            _message_with_attachment(
                valid,
                filename="evidence.docx",
                maintype="application",
                subtype=(
                    "vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
            )
        )
        self.assertEqual(
            parsed.attachments[0].content_type,
            (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )

        for spoofed in (prefix_only, manifest_without_main_part):
            with self.subTest(payload=spoofed[:24]):
                self.assert_security_error(
                    "attachment_content_type_mismatch",
                    lambda spoofed=spoofed: _parse(
                        _message_with_attachment(
                            spoofed,
                            filename="spoofed.docx",
                            maintype="application",
                            subtype=(
                                "vnd.openxmlformats-officedocument."
                                "wordprocessingml.document"
                            ),
                        )
                    ),
                )

    def test_ooxml_expanded_bytes_are_bounded(self):
        word_manifest = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/"
            b"content-types'>"
            b"<Override PartName='/word/document.xml' "
            b"ContentType='application/vnd.openxmlformats-officedocument."
            b"wordprocessingml.document.main+xml'/></Types>"
        )
        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr("[Content_Types].xml", word_manifest)
            archive.writestr(
                "word/document.xml",
                b"\x00" * (512 * 1024),
            )
        payload = output.getvalue()

        self.assert_security_error(
            "attachment_content_type_mismatch",
            lambda: _parse(
                _message_with_attachment(
                    payload,
                    filename="expanded-bomb.docx",
                    maintype="application",
                    subtype=(
                        "vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                ),
                max_attachment_bytes=256 * 1024,
                max_total_attachment_bytes=512 * 1024,
            ),
        )

    def test_ooxml_compression_ratio_is_bounded_below_expanded_size_limit(self):
        mime = _mime_module()
        word_manifest = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<Types xmlns='http://schemas.openxmlformats.org/package/2006/"
            b"content-types'>"
            b"<Override PartName='/word/document.xml' "
            b"ContentType='application/vnd.openxmlformats-officedocument."
            b"wordprocessingml.document.main+xml'/></Types>"
        )
        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr("[Content_Types].xml", word_manifest)
            archive.writestr("word/document.xml", b"\x00" * (128 * 1024))

        payload = output.getvalue()
        self.assertLess(len(payload), 128 * 1024)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            document = archive.getinfo("word/document.xml")
        self.assertLess(document.file_size, 256 * 1024)
        self.assertGreater(
            document.file_size,
            document.compress_size * mime._MAX_OOXML_COMPRESSION_RATIO,
        )

        self.assertIsNone(
            mime._ooxml_content_type(
                payload,
                max_entry_expanded_bytes=256 * 1024,
                max_total_expanded_bytes=512 * 1024,
            )
        )

    def test_ooxml_manifest_rejects_utf16_dtd_entity_expansion(self):
        content_type = (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml"
        )
        manifest = (
            "<?xml version='1.0' encoding='utf-16'?>"
            "<!DOCTYPE Types [<!ENTITY main_type "
            f"'{content_type}'>]>"
            "<Types xmlns='http://schemas.openxmlformats.org/package/2006/"
            "content-types'>"
            "<Override PartName='/word/document.xml' "
            "ContentType='&main_type;'/>"
            "</Types>"
        ).encode("utf-16")
        payload = _ooxml_payload(
            manifest,
            {"word/document.xml": b"<document/>"},
        )

        self.assert_security_error(
            "attachment_content_type_mismatch",
            lambda: _parse(
                _message_with_attachment(
                    payload,
                    filename="entity.docx",
                    maintype="application",
                    subtype=(
                        "vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                )
            ),
        )
