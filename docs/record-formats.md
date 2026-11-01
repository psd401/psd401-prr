# Record formats and how each one is reviewed

A public records collection is not a pile of PDFs. One district request pulls
in Word documents, spreadsheets, phone photos, email, bus and building camera
footage, call recordings, and the occasional dead format. Those differ in kind,
not in degree, and only one of them can be read by a page renderer.

`apps/records/handling.py` is the single place that decides which path a record
is on. The reviewer's list states the path on every row, so a reviewer learns
what is possible before spending a click rather than after hitting an error.

| Path | Meaning | How it is reviewed |
|---|---|---|
| `preview` | PDF | Opens directly; redaction burns into the file |
| `convert` | Office, images, text, email | A PDF rendition is built; redaction burns into the rendition |
| `media` | Audio and video | Player plus timeline; redaction re-encodes |
| `opaque` | Nothing can render it | A named human decides on stated evidence |

Classification uses the stored media type, falling back to the filename
extension, because files that arrived from another system routinely carry
`application/octet-stream`. An unrecognized file is `opaque` rather than
assumed reviewable: claiming a record is reviewable when nothing can open it
invites a reviewer to certify a file they never saw.

## Renditions (`convert`)

`apps/records/derivatives.py` renders the copy a human reads. Three rules stop
that from becoming a lie about the record:

* **The original is never replaced.** It keeps its own key, digest, and length.
  A record released unredacted is released as the original.
* **A failed or impossible conversion is recorded, not swallowed.** A record
  whose rendition failed stays visibly unreviewable, because an empty document
  reads to a reviewer as "nothing to withhold".
* **Conversion is bounded** by size and wall clock. These renderers take
  untrusted input from outside the district.

Redaction of a converted record burns into the rendition, and that is what the
requester receives. Releasing the original `.docx` would ship tracked changes,
comments, and author metadata that no box on a page ever covered.

Images and text render with PyMuPDF. Office formats go through headless
LibreOffice (`libreoffice-core/-writer/-calc/-impress` in the image) in a
throwaway user profile, so concurrent conversions cannot collide. Email renders
through the same searchable-PDF converter Vault ingest uses; Outlook `.msg` is
declined with instructions rather than rendered as a header dump, because no
`.msg` parser is installed.

Conversion runs when a record is created and is retried by
`build_pending_record_derivatives` every ten minutes. It is queued rather than
inline: a migration stores hundreds of files at once, and doing this inline
would turn a transfer into an hours-long job that fails as a single unit.

## Audio and video (`media`)

`apps/records/media_review.py`. Time-based media cannot be reduced to pages:
the exempt material is a face visible for eleven seconds, or a name spoken once
in a forty-minute recording. Review needs a player and a timeline.

A reviewer marks intervals, each with a treatment — obscure a region of the
frame, obscure the whole frame, mute the audio, or cut the interval — and each
with an exemption citation, exactly as on paper. `MediaRedaction` makes the
citation non-nullable for the same reason `Redaction` does: the
RCW 42.56.210(3) log is generated from these rows.

Producing the released copy re-encodes through ffmpeg. A stream copy would
change the container while leaving every frame the redaction was meant to
remove. The original is retained, and a manifest digest records exactly which
redactions were applied.

**No face detection.** An automated blur that misses one frame discloses the
student it was meant to protect, and no reviewer can verify that negative
across thousands of frames. A human marks the intervals; the machine applies
exactly what was marked.

## Records nothing can open (`opaque`)

`apps/records/opaque_review.py`. A dead format, a proprietary container, a
corrupt file. The PRA does not excuse the district from deciding.

Before this path existed there were two options and both were bad: leave the
record unreviewed forever, or mark it reviewed and leave no trace that nobody
could open it. The second is worse, because afterwards the file looks reviewed
while the reviewer certified something they never saw.

So the decision is allowed and made expensive: a named human, an explicit basis
(opened elsewhere, described by the custodian, or not inspectable by any
available means), and reasoning long enough to be an actual explanation. It is
recorded append-only next to the record. Withholding still requires an
exemption citation. The service refuses outright on a record that *can* be
displayed — if a renderer exists, the reviewer has to look at it.

## Current coverage

Measured across the 1,272 files of the migrated NextRequest corpus:

| Path | Files | Share |
|---|---|---|
| `preview` | 665 | 52% |
| `convert` | 394 | 31% |
| `media` | 2 | 0.2% |
| `opaque` | 211 | 17% |

The `opaque` share is dominated by Flash (`.swf`) and Prezi (`.keg`) assets
from one 2017 training bundle. Nothing will ever render those; they exist to be
decided on other evidence and recorded.
