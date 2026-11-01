# Build vs. Buy vs. FOSS — Research Summary (July 2026)

## Commercial baseline (what we'd be replacing/matching)
| Product | Notes | Cost signal |
|---|---|---|
| CivicPlus NextRequest | Market leader; portal, routing, RapidReview redaction, exemption logs | ~$1k–5k/month typical; enterprise $50k+/yr |
| Granicus Records Request Mgmt (GovQA) | Heavily used by schools/districts; CJIS/HIPAA-compliant hosting; email extract/dedupe/search | Enterprise pricing |
| JustFOIA | Simpler, smaller-agency oriented | Mid-market |
| Tyler FOIAXpress | Enterprise, integrates with Tyler ecosystem | Enterprise |

Feature bar to match: public portal, auto-routing, deadline automation,
redaction with logs, installments, secure delivery, dedupe, reporting.

## FOSS landscape
- **ArkCase Community Edition** — the only credible agency-side FOSS option.
  LGPL-3.0; commercial edition is FedRAMP Moderate; Montana DOA picked it for
  public information + e-discovery. BUT: heavyweight Java/Spring + Alfresco +
  Solr + separate Spring Cloud Config Server; federal-FOIA-shaped workflows;
  no Google Vault integration. Verdict: worth a 1–2 day eval spike; likely
  more ops burden than a purpose-built Django app for a single district.
- **RecordTrac** — the old Oakland/Code for America open-source portal is now
  an OpenGov commercial product; original code is unmaintained. Not viable.
- **Alaveteli / MuckRock** — requester-side platforms, not agency-side.

## FOSS components we ARE using
- `holidays` (WA calendar), Celery/Redis, MinIO (S3-compatible), and in
  Phase 2: ocrmypdf/Tesseract (OCR), PyMuPDF (true redaction — permanently
  removes text/images in redacted regions), Apache Tika (format extraction),
  ClamAV (upload scanning).

## Google Vault API — verified capabilities
- Matters, holds, searches, and exports are all fully scriptable (REST v1).
- Auth: service account w/ domain-wide delegation; `ediscovery` +
  `devstorage.read_only` scopes; impersonated user needs Vault privileges.
- Exports land in a Google-managed Cloud Storage bucket; download via the
  Cloud Storage API (`cloudStorageSink.files` lists bucket/object names).
- MBOX exports process faster than PST — we use MBOX.
- Requires a Workspace edition that includes Vault (Education Plus or the
  Vault add-on) — **confirm PSD401's license tier before Phase 2.**

## Recommendation
Custom Django build (this repo) with FOSS components. Rationale: RCW 42.56-
native workflows, first-class Vault integration no vendor offers a district
this cheaply, in-house engineering capacity exists, and avoided license cost
(~$12–60k/yr) funds the build. Keep the ArkCase CE eval as a checkpoint before
committing to Phase 2.
