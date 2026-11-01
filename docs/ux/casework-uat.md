# Casework acceptance record and manual UAT checklist

## Scope and current decision

Issue #154 uses the deterministic local `PRR-E2E-2000` fixture. It contains
exactly 2,000 synthetic messages plus role-specific collection, review,
delivery, and lifecycle evidence. No production data, requester
communication, or production mutation is used.

Automated acceptance status: **PASS** for the recorded local run on
2026-08-24. The browser suite executes persisted authorized outcomes and
retains screenshots and traces. It is repeatable CI evidence, not a substitute
for the manual acceptance items marked `(manual)` in issue #154.

Manual UAT status: **PENDING**. A named Records Officer, Department Liaison,
and Reviewer must complete the sessions below without facilitator prompts.
Until their observations and decisions are entered here, issue #154 and the
production rollout decision remain blocked.

## Automated role journeys

| Role | Persisted synthetic journey | Result | Evidence |
| --- | --- | --- | --- |
| Records Officer | Enter from assigned work, open source context, record and reopen an inbound No action disposition, find sent correspondence, and inspect Collection, Records, Releases, and Activity | Pass | `complete-officer-case-journey` |
| Department Liaison | Enter from assigned work, advance and restore the scoped collection task, edit a draft, then verify that a Records Officer receives the approval handoff while liaison approval stays unavailable | Pass | `complete-liaison-handoff` |
| Reviewer | Enter from assigned review work, keep requester PII and Messages unavailable, record a responsive outcome on the dedicated UAT record, and preserve dialog context through Back/Forward | Pass | `complete-reviewer-journey` |

## Required manual role sessions

Use a fresh local fixture and the staff guide. Do not tell the participant
which tab, filter, route, or control to use. Record the participant's name,
date, exact build SHA, observations, severity, and disposition. Every P2 must
be explicitly accepted for rollout or deferred with an owner and target date.

| Role | Required unaided outcome | Operator/date/SHA | Observations and P0/P1/P2 decisions | Result |
| --- | --- | --- | --- | --- |
| Records Officer | Identify the next action, find `Synthetic outbound message 0002` in the 2,000-message case, verify what was sent, explain the existing No action disposition, and identify the human-only approval handoff | Pending | Pending | Pending |
| Department Liaison | Open assigned work, update the collection task, prepare the existing synthetic draft for officer approval, and explain why approval/camera exclusion is unavailable | Pending | Pending | Pending |
| Reviewer | Open assigned review work, record an outcome on the dedicated UAT item, and confirm that requester PII and correspondence remain unavailable | Pending | Pending | Pending |

The named Records Officer must also state whether **Releases** is an
understandable tab label after reading its supporting text. Record that as an
explicit accept-or-defer P2 decision; the prior design decision is not a proxy
for operator acceptance.

## Accessibility and interaction evidence

| Check | Automated result | Manual status | Recorded evidence |
| --- | --- | --- | --- |
| axe on login, shell, Inbox (message triage and assignment queues), all six case tabs, message detail, and every action/confirmation dialog reachable in the deterministic case | Pass; zero serious or critical findings, with complete violation details retained | Not applicable | `.verification/axe-report.json` |
| Keyboard shell, skip link, Inbox filter, message selection, assignment, read/unread, persisted No action/reopen, focus-loop boundaries, error focus, tabs, and Back/Forward | Pass | Not applicable | `keyboard-case-inbox` |
| Polite queue and new-message announcements without focus, scroll, or selected-row movement | Pass | Not applicable | `accessible-live-updates` |
| 320 CSS-pixel reflow at Cases, Inbox, and a case journey | Pass by browser measurement | Named staff visual check pending | accessibility screenshots and axe JSON |
| 200% zoom at Cases, Inbox, and a case journey | Pass by the 320 CSS-pixel reflow equivalent of a 640-pixel reference viewport | Named staff visual check pending | accessibility screenshots and axe JSON |
| Reduced motion and forced-colors at the same three surfaces | Pass by browser emulation | Named staff visual check pending | accessibility screenshots and axe JSON |
| Screen-reader structure and complete task narration | ARIA snapshots retained for all three surfaces | **Pending a named screen-reader user/operator** | `.verification/axe-report.json` plus this record |

Automated checks are deliberately labeled automated. They do not claim that a
screen reader user inspected the experience or that a human completed the
manual UAT.

## High-volume acceptance

The browser keeps one 50-row page mounted in message, thread, and transport
views. It proves unique IDs across loaded pages, observes every response under
256 KiB, and loads no detail body before selection. The PostgreSQL gate follows
all message cursors to exactly 2,000 unique IDs and verifies the representative
search projection before timing 30 summary and 30 full-text-search samples.

## Automated findings and decisions

- Automated P0 findings: **0**.
- Automated P1 findings: **0** after the self-review fixes were applied.
- P2: low-contrast secondary/link colors. **Fixed** and covered by axe.
- P2: new-arrival insertion moved the selected summary. **Fixed** with a
  reserved update region and selection/focus/viewport assertions.
- P2 design decision: retain **Releases** as the tab label. **Provisionally
  accepted by design; manual Records Officer acceptance is still pending.**

Release-gate result: **BLOCKED on named manual UAT and screen-reader
validation.** Passing automation, code review, or CI cannot change that manual
status. Production remains separately governed by the rollout record and the
canonical runbooks.
