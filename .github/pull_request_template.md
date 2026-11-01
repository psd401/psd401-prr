## What changed

<!-- Describe the user outcome and why this change is needed. -->

## AI disclosure

<!-- Required. e.g. "Claude Code wrote the implementation; I reviewed and tested" / "No AI assistance" -->

## Verification

<!-- List the tests and manual checks you ran, with their results. -->

## Required checklist

- [ ] I wrote a failing test first for every feature or bugfix.
- [ ] I ran `python manage.py test apps --settings=config.test_settings`.
- [ ] I ran `bun run test`, `bun run lint`, and `bun run build` in `frontend/` when frontend code changed.
- [ ] Every new staff GUI outcome uses an agent-accessible atomic REST primitive and is registered in `apps/api/capabilities.py`.
- [ ] I reviewed each new or changed capability's risk, execution policy, and human handoff; I did not bypass the human-only statutory rules in `AGENTS.md`.
- [ ] I kept domain and compliance logic in Django rather than frontend or agent orchestration code.
- [ ] I updated the owning hand-written documentation; I did not hand-edit generated `openwiki/` pages.
- [ ] I did not read, modify, or commit `.env` or another secret-bearing file.
- [ ] Claude posted an actual review of the latest commit (not only a green workflow check), and I triaged every finding (fixed it, or dismissed it with a one-line reason).

<details><summary>Reviewer checklist for AI-authored tests (expand if tests changed)</summary>

- Would this test fail if the behavior broke?
- Are assertions specific (exact values, not just "defined")?
- Is the unit under test not mocked away?
- Real edge cases (empty/zero/negative/error paths)?
- Any existing test weakened or deleted? (Must be declared above.)
</details>
