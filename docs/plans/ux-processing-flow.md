# Plan: an app people enjoy using

Status: proposed, 2026-11-06. Based on a signed-in walk through production.

## Is it a pleasure to use today?

No. It looks better since the design system went in, but working a request
feels like hunting:

- **It's slow.** The Inbox takes several seconds to load.
- **Nothing says what to do next.**
  - The Inbox shows 83 cases at the same weight.
  - The Review page is 2,993 identical cards.
- **On a case:**
  - Three places name the next step, and they disagree.
  - The button that does it is on another tab.
- **There's too much to read.**
  - A long email chain fills the screen before any action.
  - Paragraphs of rules and developer notes sit on every panel.
- **Reviewing is tedious.**
  - Emails show raw HTML.
  - Records are named "Vault message.pdf".
  - Duplicates aren't grouped.
  - AI suggestions are switched off.
- **There are too many choices.**
  - Six navigation items.
  - Seven Inbox views plus "More views".
  - Six buttons on the intake stage.
  - 100+ settings in one scroll.

## What it should feel like

You open it, see the next thing to do, do it in one click, and move on.

## Plan

### 1. One home screen: "Today"

- One list of open cases. Each row has three parts:
  - a short AI title;
  - the one next step, as a button;
  - when it's due.
- The most urgent case is at the top.
- One search box, and no view tabs.
- Navigation becomes **Today**, **New request** and **Settings**.
  - Review, Decisions and Oversight fold into the case or into Settings.

### 2. A case is one calm page

At the top:

- the AI title;
- a two-line summary of what was asked;
- the one next step, as a large button, such as "Send acknowledgment",
  "Start collection" or "Review 2,324 records".

Below that, the stage rail.

Everything else sits under "Details": the full request text, logs, notes and
other actions.

### 3. Review that moves fast

- Full-screen, one record at a time:
  - a readable email: plain text, subject as the title;
  - duplicates folded into one;
  - the AI's suggestion shown;
  - nothing pre-selected.
- One key to decide: **r** responsive, **n** not.
  - It moves straight to the next record.
  - A progress bar shows how far through you are.
  - A clear "All done" at the end.
- Whole groups of look-alike records can be decided at once, with a quick
  sample check.

### 4. Turn the AI on so the screens have something in them

These are your six switches; no code is needed:

1. **Classify new cases automatically.** Suggests new request, follow-up,
   duplicate, not a request or spam.
2. **Prepare request-intent proposals automatically.** Drafts the search
   scope and runs a count.
3. **Prepare record proposals automatically.** Suggests responsive or not
   on each record.
4. **Draft interpretation-and-estimate letters automatically.** Drafts only;
   you approve and send.
5. **Day-5 acknowledgment safety net.** Sends the acknowledgment if nobody
   has by day 5.
6. **Allow automated correspondence to send.** Lets #5 and the submission
   receipt actually go out.

### 5. Polish

- Every page loads in under a second.
- Saves happen instantly, with a short confirmation.
- One date style everywhere.
- Plain, short wording; no rule or developer text on screen.
- Settings has search, and a short "Main" section with the rest under
  "Advanced". Model choices are a dropdown, not typed in.

## Order

Each step ships on its own: 4, then 1, 2, 3, 5. Step 4 is your switches and
gives the other screens content.

## Done when

| Criterion | Status |
|---|---|
| From sign-in, the next step on the top case is one click away | PENDING |
| A case page shows title, summary and one next-step button above the fold | PENDING |
| A record review is one keypress and advances automatically | PENDING |
| Navigation has three items | PENDING |
| Inbox and case pages load in under 1 second in production | PENDING |
| All existing tests pass | PENDING |
