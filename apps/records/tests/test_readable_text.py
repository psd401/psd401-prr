"""A record's text, cleaned for a person to read during review.

The stored ``ocr_text`` is left exactly as extracted (search reads it); the
reading copy drops markup an email client would never show.
"""

from django.test import SimpleTestCase

from apps.records.readable import readable_text


class ReadableTextTests(SimpleTestCase):
    def test_drops_outlook_conditionals_tags_and_styles(self):
        raw = (
            "Subject: The Tides Table\n"
            "<!--[if gte mso 9]><v:background fill=\"t\"></v:background><![endif]-->\n"
            "<style>td { padding: 0 }</style>\n"
            "<table role=\"presentation\"><tr><td>Hey Tides</td></tr></table>\n"
            "<p>Welcome&nbsp;back &amp; enjoy</p>"
        )
        self.assertEqual(
            readable_text(raw),
            "Subject: The Tides Table\n\nHey Tides\n\nWelcome back & enjoy",
        )

    def test_keeps_link_and_heading_words_but_not_their_markup(self):
        raw = (
            "![Tides Table Header](https://cdn.example.org/header.png)\n"
            "## WELCOME BACK\n"
            "Read the [fall calendar](https://example.org/cal) today.\n"
            "|\n"
            "|"
        )
        self.assertEqual(
            readable_text(raw),
            "WELCOME BACK\nRead the fall calendar today.",
        )

    def test_collapses_runs_of_blank_lines_and_trailing_space(self):
        self.assertEqual(
            readable_text("One   \n\n\n\n\nTwo​\n"),
            "One\n\nTwo",
        )

    def test_plain_text_is_unchanged(self):
        text = "Date: Fri, 28 Aug 2026\nFrom: someone@example.org\n\nThank you."
        self.assertEqual(readable_text(text), text)
