# Casework staff guide

This guide covers the staff workspace for Public Records Act casework. The
system keeps legal status, message transport, personal read state, and team
work disposition separate. A convenient UI action never changes another one
by implication.

## Start with the queue for your role

There is one work surface. The Inbox carries cross-case requester messages, the
statutory backlog, and every queue that used to live on a separate My Work
screen; `/my-work` now redirects into it.

- **Records Officers** land on the **Inbox**. Triage messages under *Needs
  you*, then use *Your queues* — Assigned to you, Unassigned, Team, and
  Critical — for work of every other kind. Each row states where its case
  stands and carries its own actions.
- **Department Liaisons** land on the Inbox with *Your queues* only; there is
  no cross-case message triage for the role. A liaison can update authorized
  collection work and prepare eligible drafts, but approval, statutory
  decisions, and human-only actions stay with a Records Officer.
- **Reviewers** land on **Review** and open assigned record work directly in a
  case's Records tab. The reviewer view does not expose requester contact or
  correspondence.
- **Intake Clerks** use **Staff Intake**. A submitted request returns its
  request number but does not grant access to case browsing.
- **Application Admins** use Configuration and authorized operations. The role
  does not imply Records Officer authority.

Queue counts update while the page is visible and when focus returns to the
window. Screen readers receive a polite summary; the application does not move
focus or scroll the current selection.

## Triage the Inbox

The Inbox groups its views by what they ask of you rather than listing them all
as equals:

- **Needs you**: Needs attention, Awaiting statutory response, New, Unmatched,
  Quarantined, and Delivery exceptions.
- **Your queues** (every kind of work, not only messages): Assigned to you,
  Unassigned, Team, and Critical.
- **In progress**: Replies waiting, and Waiting on others.
- **Reference**: Received, Handled, No action, Recently changed, and All.

**Awaiting statutory response** lists every received request that has no
delivered five-business-day response under RCW 42.56.520, soonest due first,
with its due date on each row. A draft or an approval does not satisfy the
deadline; only a confirmed delivery does. The Overview tab of each of those
cases shows the ordered steps that remain — prepare the draft, approve the
exact wording, queue it, and wait for the mail provider to confirm — along with
the single next action.

A Records Officer message row is named by its subject line and states, without
opening anything, the message kind, its timestamp, the attachment count, a
bounded snippet, the case number, that case's legal status, its statutory
response position, and the response due date. Full bodies, attachments, and
requester identity still open only inside the authorized case. The Application
Admin Inbox stays limited to sanitized delivery-exception work with no
requester or message content.

Each row also carries the dispositions an officer needs:

- **Open case** goes to that message inside its case.
- **Mark handled** and **No action** set team work state; neither changes legal
  status, and No action requires a written reason.
- **Not a records request…** appears only on a case that was auto-created from
  an unmatched email. It asks for a written reason, closes the case under the
  retained-evidence closure, and marks the row handled in the same action.
  Nothing is deleted and no requester message is sent. There is no automatic
  version of this decision and none will be added.

**Prepare acknowledgment** on an *Awaiting statutory response* row opens the
case's Messages tab with the acknowledgment composer already open. The Messages
tab shows the same four-step statutory tracker as Overview, with a button for
the step that is actually outstanding: prepare the draft, review and approve
it, or open the approved message to queue it. Each remains a separate,
human-confirmed action, because a draft is not a response and neither is an
approval.

## The Cases table

The **Cases** and **All cases** views list the cases themselves rather than the
messages on them, which is the only way to see a web-form request that has no
email row anywhere.

**Cases** lists open cases only. **All cases** opens on **All open** too; its
status menu also offers **All closed** (Fulfilled, Denied, and every Closed
status), **Every status**, and any single status. The message views (Needs
attention, Handled, All messages, and the rest) likewise show mail on open
cases by default, with a **Case state** menu for closed cases or every case.
Their chip counts are the open-case numbers.

A case with requester mail nobody has handled yet is marked the way unread mail
is marked: a dot beside the case number and a bold row. Hovering the dot, or
reaching it with a screen reader, says how many messages are waiting. The
row's **⋯** menu opens the case or marks it caught up; marking caught up
disposes every waiting requester-email row on that case at once and changes
nothing about the messages or the case's legal status. Selecting several cases
and using **Mark caught up** in the selection bar does the same for each of
them, one at a time, so each keeps its own audit entry.

Clicking anywhere in a row opens the case, and so does pressing Enter on a
focused row. A click that lands on the checkbox, a link, or the row menu
belongs to that control instead.

**Next due** is the date the case owes an answer by next, with a small label
saying under which rule:

| Label | What it means |
|---|---|
| **5-DAY** (amber) | The RCW 42.56.520 five-business-day response has not been sent. Strict liability; this is the one that is law. |
| **Estimate** | The completion date the District promised this requester. |
| **Target** | The District's own working target, 60 calendar days from the first response, used where no estimate was given. Not a statutory deadline. |
| **FERPA access** | The 34 CFR 99.10(b) 45-calendar-day access date for a parent or eligible-student matter. |

A litigation matter shows no date here: there is no requester and no statutory
clock, and counsel's date lives in the **Estimated** column instead.

**Subject** is a one-line label, never the request itself — the request wording
is the legal record and is never edited. For a Records Officer it is the AI
case title where one has been proposed, marked **AI**; for everyone else, and
for any case without one, it is the opening of the request text.

**Latest note** shows the most recent internal note, with its author and time
on hover.

Sorting a column asks the server to reorder the whole result set, not the page
on screen, and the ordering goes in the URL so a sorted list is a link you can
send to a colleague. A notice above the table says which order is showing and
offers **Restore default order**, which returns to flagged-first, then soonest
due.

## Work inside a case

The case header identifies the request and exposes only information allowed for
the current role. Each tab has one purpose:

- **Overview**: immutable request text, owner, deadlines, lifecycle progress,
  intake attachments, time entries, and authorized legal actions.
- **Messages**: bounded message summaries, search, selected-message detail,
  drafting and transport actions, and message work state.
- **Collection**: collection assignments, preservation work, Vault handoffs,
  and advisory intent. Camera exclusion remains a reasoned human action and
  footage is never auto-denied.
- **Records**: uploading records you already have, responsiveness review,
  redaction, exemption citations, and second review.
- **Releases**: installments, fees, packages, exemption-log readiness,
  downloads, and the read-only closure blocker report. Opening this tab does
  not release records or close the case.
- **Activity**: append-only lifecycle, work, and delivery events. It is
  read-only and is not a substitute for the work queue.

The description on Overview is the legal record of what was requested and is
never edited after intake. Clarification creates new evidence instead.

### Closing a case

**Close case…** on Overview is available to everyone who works cases here, not
only Records Officers. It records one of two endings — *Closed – Withdrawn* or
*Closed – Abandoned* — and requires a written reason, which goes on the
timeline and into the audit trail under your name. If the case is awaiting
clarification, closing it cancels the open cycle at the same moment and
fabricates no response.

**Change status** is a different control and stays with the Records Officer:
it moves a case between working states, and it does not offer withdrawal or
abandonment. Fulfilment, denial, no responsive records, closing an email as
not a records request, and reopening a closed case all remain Records
Officer decisions.

### Uploading records to a case

When you already have the files — a department emailed them, a custodian
handed over a drive, you scanned a paper file — use **Upload records** at the
top of the **Records** tab. It is a Records Officer action; reviewers and
admins see the tab without the button.

Choose one or more files and select **Upload**. Each file is scanned for
malware and stored unchanged before it appears in the list as an
**Unreviewed** record with source system `upload`, ready for the same review,
redaction, and release steps as anything Vault collected. The dialog then
shows what happened to each file:

- **Added** — the file is now a record on this case.
- **Already on this case** — a byte-identical file is already here, so no
  second copy was made.
- **Rejected: malware detected** — nothing was kept.
- **Rejected: scanning unavailable** — the scanner could not be reached, so
  the file was refused rather than accepted unscanned. Try again later, and
  tell the application administrator if it persists.
- **Rejected: empty file** — the file had no content.
- **Not added: case was closed** / **Not added: no longer authorized** — the
  case closed, or your Records Officer role was removed, while the upload was
  running. Files listed as **Added** before that point stay on the case; the
  rest were not added.

The district sets how many files one upload may carry and how large each file
and the whole upload may be (Configuration > District settings, under
Records handling). An upload over a limit is refused as a whole before
anything is stored. A closed case does not take new records: reopen it first.
Each added record is written to the audit log under your name with the file
name, size, and SHA-256 digest; the record's custodian stays blank because it
names where a record was found, not who uploaded it.

### An email that belongs to a case you already have

When a requester follows up from a fresh email thread, intake opens a new case
for it rather than guessing which existing case it belongs to. When you
recognize the real case, use **Add to an existing case…** — from the Inbox row
or from the auto-created case's Overview. Give the case number and say why.

That is one action, not two: the message and all of its attachments move to
the case you name, and the leftover case closes as not a records request with
your reason and the destination case number on its timeline. Nothing is
deleted and no requester message is sent. If either half cannot be done,
neither is.

### Internal notes

Every case has an **Internal notes** panel on Overview: what you did, who you
spoke to, what the case is waiting on. Anyone who can open the case can write
one, and the newest one shows in the Cases table so the next person scanning
does not have to open anything to find out what happened last.

Three things are worth knowing before you write one:

- **It is never sent.** No letter, production, or export includes a note.
- **It cannot be edited or deleted.** A note records what somebody knew and
  when. Correct one by writing another that says so, the way you would in a
  paper case file.
- **It may itself be a public record.** "Internal" means it is not part of
  this case's response to this requester — not that it is confidential. A note
  about a public records request is very likely disclosable on a request for
  it. Write it so you would not mind reading it back.

The running log beside it records what the *system* did; notes record what the
*people* did.

## Read and handle messages

Message search and filters are server-side. At most 50 summaries are mounted at
once; use **Load more** and **Show newer** to move between bounded pages. A full
message body is fetched only after selecting its summary.

Compose offers a **Submission receipt (courtesy)**. It confirms to the
requester that the District received the request; it is explicitly not the
five-business-day response and does not change the deadline or the request
status. Only one may exist per request. Authored message types (general,
responsive records, closure, denial) offer **Start from district template**,
which fills an editable starting point that you remain the author of.

The labels answer different questions:

| Label | Meaning |
| --- | --- |
| New / Read | Your personal receipt state. It does not affect teammates. |
| Needs attention | The team still has an active next action. |
| Waiting | The team is blocked on an external or later event. |
| Handled | The team action is complete. |
| No action required | A reversible Records Officer decision with a category and substantive reason. |
| Draft / Approved / Queued / Sent / Failed / Cancelled / Received | Message transport evidence, not request lifecycle. |

Use **Mark read** or **Mark unread** for your personal state. Use **Set waiting**,
**Mark handled**, or **Reopen team work** for shared disposition. **No action
required** is Records Officer-only, requires a substantive reason, and can be
reopened. None of these actions changes the request's legal status.

New arrivals are staged. The polite announcement says how many are available,
and **Show new messages** applies them when you choose. Your focused message,
selection, and scroll position remain stable until then.

## Keyboard and assistive technology

Use **Skip to main content** to bypass navigation. All shell links, queue
filters, summaries, actions, tabs, and dialogs support ordinary Tab,
Shift+Tab, Enter, Space, and Escape behavior. The selected queue and message
also have non-color state through `aria-pressed`, text, borders, and badges.

Dialogs move focus to their first field, keep focus inside while open, close
with Cancel or Escape when safe, and restore focus to the trigger. Validation
errors receive focus through the shared error presentation. At 200% zoom and a
320 CSS-pixel viewport, use the responsive menu and normal vertical scrolling;
horizontal page scrolling is not required.

## Compatibility links and help

Existing scoped bookmarks for Tasks, Preservations, Record items,
Installments, Time entries, and Correspondence redirect to the canonical case
tab while preserving validated filters or selection. Back and Forward remain
usable because obsolete routes are replaced in browser history.

If an action is missing, first confirm the signed-in role and the case tab. A
Not Authorized page is a server-enforced boundary, not a navigation problem.
For failed message transport, use Configuration → Correspondence operations;
for legal or statutory judgment, hand the case to a Records Officer.
