# Shared agent protocol — EEUU Trend Finder

This file applies to Codex and Claude Code. **The existing `CLAUDE.md` is authoritative for project methodology and non-negotiable requirements.** Do not weaken or replace it; read the relevant sections before decisions involving statistical methods, ranking, ingestion or evaluation.

## Start of each task (bounded context)
1. Read `docs/STATE.md` (short, operational snapshot).
2. Identify the scope and the relevant modules. Do not read old conversations or the whole repository as a default.
3. If Graphify is installed and its graph exists, use a narrow `graphify query "<specific question>"` before broad code searches. Graph results are navigation hints, **not** evidence that code works.
4. Read the actual source and relevant ADRs in `docs/decisions/` before changing behavior.
5. If Graphify is absent or stale, use targeted search and source reads; do not block productive work to rebuild the graph.

## Constraints
- Keep `CLAUDE.md` methodology intact: no hardcoded topics, strict isolation of `eval/`, calibrated FDR and multiple-testing control, independent-source triangulation and historical vintage integrity.
- Do not silently change scientific definitions, ranking eligibility, data access limits or published evidence. Request approval for decisions that affect those invariants.
- Never reveal, commit or put secrets, sensitive data, or raw exported conversations into agent memory or Graphify outputs.
- Preserve the existing ADRs as the canonical record of design decisions; do not create a competing `DECISIONS.md`.

## Task completion and handoff
For any substantial implementation:
1. Run relevant tests and record **actual** commands and outcomes. Use `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` where supported.
2. Update `docs/STATE.md` with verified state, blockers, the latest relevant commit/PR, and a single next action. Keep it concise; distinguish claims from verified results.
3. Add a new ADR in `docs/decisions/` only for a lasting architectural or methodological decision; link it from STATE if relevant.
4. State changed files and unresolved issues in the handoff. Do not paste full conversation transcripts.
5. A fresh session should be able to resume using STATE, applicable ADRs and targeted code queries.

## Concurrent agents
- Claude Code and Codex should use separate branches/worktrees when working concurrently.
- Check `git status` and current branch before edits, don't overwrite uncommitted changes, and reconcile STATE updates after merge.
- Do not claim tasks are complete or tests pass without verified evidence.

## Graphify
- Installation and regeneration instructions: `docs/agent-context.md`.
- The generated graph is a local, disposable index, not a canonical source of truth. Do not commit it.
- Do not rebuild on every message; regenerate when code materially changes or when targeted queries are demonstrably stale.
