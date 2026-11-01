---
type: Domain
title: Record Email Facts, Custodians, and Review Tags
description: The facts PSD401 PRR stores about each retained email record - parsed headers, body digest, and the mailboxes a record was found in - and the deterministic review tags derived from them. Explains why these facts never decide responsiveness, exemption, or privilege.
tags: [domain, email-metadata, custodians, review-tags, vault-ingest, privilege-queue]
openwiki:
  roles: [domain, integration]
  change_kinds: [email-metadata, review-tags, custodians]
  source_paths: [apps/records/email_metadata.py, apps/records/record_tags.py, apps/records/vault_metadata.py, apps/records/vault_ingest.py, apps/api/resources/counsel_domains.py, apps/records/management/commands/backfill_record_email_metadata.py, apps/records/management/commands/backfill_vault_custodians.py, apps/records/management/commands/fold_duplicate_record_copies.py]
  symbols: [parse_email_metadata, EmailMetadata, as_record_item_fields, parse_delivered_to, tag_queries, annotate_tags, filter_by_tag, tags_for_item, RecordItemCustodian, CounselDomainViewSet]
  test_paths: [apps/records/tests/test_review_throughput.py, apps/records/tests/test_vault_custodian_metadata.py, apps/records/tests/test_vault_ingest.py, apps/api/tests/test_review_throughput_api.py]
  invariants:
    - Email facts describe the message only; no fact is a responsiveness, exemption, or privilege decision.
    - A tag and the filter for that tag are the same predicate, so a row and a filter cannot disagree.
    - Tags that route to the privilege queue never withhold a record by themselves.
  validation_commands: ["PYTHON_DOTENV_DISABLED=1 python manage.py test apps.records.tests.test_review_throughput apps.records.tests.test_vault_custodian_metadata --settings=config.test_settings"]
---

# Record Email Facts, Custodians, and Review Tags

Retained email records are described by facts, not judgments. `apps/records/email_metadata.py`
turns a raw message into stored header facts. `apps/records/record_tags.py` turns those facts
into labels a reviewer can sort by. Neither module decides what a record means legally. The
module docstrings state this boundary: every legal decision stays with a named human.

The same extraction runs in two places, so a record created today and a record migrated from a
retained raw message describe themselves the same way. Vault ingest calls it when a message is
processed, and the backfill commands call it for records created earlier.

## Email facts

`parse_email_metadata(raw_message)` returns an `EmailMetadata` value with:

- `message_id` and `in_reply_to`, bounded to the RFC 5322 width
- sender, recipients, and CC lists, plus `sender_domain`
- `sent_at`, parsed from the Date header
- `auto_submitted`, `list_id`, and `precedence`, which carry the machine-generated and bulk signals
- `has_calendar_part`, set when a calendar part is present
- `body_sha256`, a digest of the normalized body text

`body_sha256` is what makes duplicates detectable. Two copies of one message delivered to
different mailboxes share it. Two different messages that happen to carry the same Message-ID
do not. `EmailMetadata.as_record_item_fields()` maps the value onto the `RecordItem` columns
added by migration `0042_record_email_metadata_and_custodians`.

Header values are cleaned of control characters and bounded by the same widths as the model
columns, so a hostile header cannot overflow a column.

## Custodians

A custodian is a mailbox or system a record was actually found in. `RecordItemCustodian` stores
one row per location. `parse_delivered_to` reads the Delivered-To header to name the mailbox for a
message, and `vault_metadata.py` reads the export's metadata CSV so one message can name every
account it was delivered to. `vault_ingest.py` attaches custodians while it processes messages
(`attach_custodians`, `attach_csv_sets_to_sole_records`). See
[collection and Vault](collection-and-vault.md) for the export path that feeds them.

Migration `0042` split earlier joined custodian strings into rows. The
`backfill_vault_custodians` command plans and applies custodian changes for existing tasks, and
`backfill_record_email_metadata` fills the email columns for records created before extraction
existed. `fold_duplicate_record_copies` uses the same message identity and body digest to find
copies of one message that may be folded, and `_ineligible` rejects any copy that cannot be folded
safely. Where a command offers a dry-run option, use it before committing a change.

## Review tags

`apps/records/record_tags.py` defines six tags. Each tag is a `Q` predicate, defined once:

| Tag | Meaning |
|---|---|
| `auto_reply` | Machine-generated: `Auto-Submitted`, a bulk `Precedence`, or an out-of-office subject |
| `calendar` | A calendar part or `.ics` attachment |
| `bulk_list` | Came through a mailing list or bulk sender |
| `external_sender` | Sending domain is not one of the district's mail domains |
| `counsel_domain` | A law firm on the administrator-managed counsel list took part |
| `privilege_marker` | Text or subject carries an attorney-client or work-product marker |

`tag_queries(counsel=...)` returns the predicate for each tag. `annotate_tags` attaches them to a
queryset as `tag_<name>` annotations, and `filter_by_tag` filters on the same predicate. Display and
filtering cannot disagree. `tags_for_item` returns the tags for one record.

`PRIVILEGE_TAGS` (`counsel_domain` and `privilege_marker`) put a record in the privilege queue, so
a human sees it before anything ships. A tag never withholds anything. Withholding still needs
an exemption citation and second review, as described in
[record review and handling](record-review-and-handling.md).

The counsel list is the `CounselDomain` model, seeded empty. Until it is populated, the
`counsel_domain` tag cannot match. Application Admins maintain it through
`CounselDomainViewSet` in [`apps/api/resources/counsel_domains.py`](../../apps/api/resources/counsel_domains.py),
served at `/api/staff/counsel-domains/`. Each entry must be one exact email domain (a leading
`@` is stripped), and each create, update, and delete writes a `records.counsel_domain_*` audit
event. The endpoint is admin-only and not available to read-only agents. Changes to that list are
a configuration decision, not a code change. Focused tests: the counsel-domain cases in
`apps/api/tests/test_review_throughput_api.py`.

## Change guidance

- Add a new header fact in `email_metadata.py` and extend `EmailMetadata` and
  `as_record_item_fields()` together. Backfill commands must reach the new field, and the
  migration must not invent values for old rows.
- Add a tag by adding one `Q` in `tag_queries`, a name in `TAG_NAMES`, and a label in
  `TAG_LABELS`. Do not add a second filter path. Test that the tag and its filter agree.
- Keep tags advisory. A new tag that could cause a record to be withheld or released needs the
  privilege or human-review path, not a shortcut.
- Use `apps.records.tests.test_review_throughput` for tag and header behavior, and
  `apps.records.tests.test_vault_custodian_metadata` for custodians. Ingest idempotency is in
  `apps.records.tests.test_vault_ingest`.
