# Flaky tests

A flaky test is one that passes and fails on the same commit. It is a defect in
the test or in the code's timing, never an excuse: a suite nobody trusts stops
being a gate. This file is the register required by psd-dev-standards
`standards/05-testing.md` rule 6, and the contract test
`apps/api/tests/test_flaky_register.py` keeps it honest.

**Retries are a detection signal, never a policy.** `frontend/playwright.config.ts`
keeps `retries: 1` with `failOnFlakyTests: true`, so a test that needs its
retry still fails the run and shows up as flake rather than hiding.

## Budget

Suite flake above **2% of runs** is an incident, not a backlog item: stop
adding quarantines and fix the suite.

## Quarantine process

Quarantine means the test **still runs but no longer gates a merge**. Nothing is
deleted, skipped, or `.only`/`.skip`-ed into silence.

1. **Confirm the flake.** Two outcomes on one commit. A test that fails every
   time is a bug report, not a quarantine.
2. **Triage before quarantining.** Read the failure, not just the summary. A
   root cause you can fix today beats a two-week quarantine — see the log
   below for what a triage writes down.
3. **Tag it.**
   - Playwright: add `@quarantine` to the test title, e.g.
     `test('officer-thing @quarantine', ...)`. The gating lanes run
     `--grep-invert @quarantine`; `scripts/run-casework-acceptance.sh --quarantine`
     runs exactly the quarantined specs and does not gate.
   - Django: `@tag("quarantine")` on the test or its class. Every gating lane
     passes `--exclude-tag=quarantine`.
4. **Open an issue** owning the fix, and add a row below with the owner, an
   expiry date **no more than four weeks out**, and the **evidence**: links to
   the two CI runs on the same commit, one red and one green. This column is
   the control that keeps quarantine from becoming a way to silence a test
   that fails every time — `test_flaky_register.py` requires it, and a
   reviewer should read those two links before approving the row.
5. **Expiry is enforced.** `test_flaky_register.py` fails once a row's expiry
   has passed, so a quarantine cannot quietly become permanent. Fix it, or
   re-triage it in the open with a new date and a reason.
6. **Leaving quarantine** means removing the tag and the row, and moving the
   entry into the triage log with what the root cause turned out to be.

## Active quarantine

None.

| Test | Lane | Owner | Issue | Quarantined | Expires | Symptom | Evidence |
|------|------|-------|-------|-------------|---------|---------|----------|

## Triage log

### `officer-high-volume-messages` — resolved 2026-10-14 (#296)

- **Observed** 2026-10-10 on `main` at `5b29512`, CI run 38013998314: failed,
  then passed on retry #1, and `failOnFlakyTests` failed the run. One
  occurrence.
- **Reported errors**, both from the same attempt:
  - `response.body: Protocol error (Network.getResponseBody): No data found for
    resource with given identifier`, raised inside the spec's
    `page.on('response')` body reader.
  - ``expect(getByText('Display-only provider thread')).toBeVisible()`` — element
    not found.
- **Root cause.** The spec asserts on response *size* as well as shape, so it
  has to read bodies, and Chromium's network store is the only source of those
  bytes. It holds nothing for a request the renderer cancelled — React Query
  aborts an in-flight message-list fetch when the workspace remounts — so the
  deferred `response.body()` rejects. The rejection surfaced as a test error
  and tore the attempt down, which is why the later thread assertion was
  reported as a second failure rather than as the cause.
- **Fix.** `responseEvidence()` in
  `frontend/e2e/officer-high-volume-messages.spec.ts` now resolves to `null`
  when the payload is gone, and the assertions count only responses the page
  actually received. Every count, size, and PII assertion is unchanged; a
  cancelled response is no longer evidence, because it delivered nothing.
- **Not quarantined**, because the cause was fixed in the same change. If the
  thread assertion fails again on its own, it is a distinct flake and gets its
  own triage and, if needed, a row above.
