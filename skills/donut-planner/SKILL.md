---
name: swim-lane-planner
description: Research this project and write small self-contained swim-lane implementation tickets. Use when asked to plan work for Donut; do not implement tickets during planning.
---

# Swim-lane planner

Read project instructions and actual code before writing tickets. Describe the user's intended change, not an invented redesign. Preserve unrelated work. Do not implement unless the user explicitly asks you to switch roles.

Tickets live in the session's lanes folder that Donut names in your assignment, `.donut/<session>/` (older projects: `swim_lanes/`); lane names below are inside it, and other sessions' folders are not yours to touch. Write Markdown tickets in its to_do/. Use NNN_YYYYMMDD-HHMM_specific-slug.md with a unique order number across all lanes. Append after the highest number; never renumber in_progress, done, or on_hold. Reference dependencies by slug, not number. Inspect existing tickets to avoid duplicates.

## On-hold tickets

`on_hold/` contains user-parked tickets, not completed work. Preserve their filenames, numbers and slugs. Reserve their numbers when creating tickets and inspect them to avoid duplicate work. Move held tickets back to `to_do/` only when the user explicitly requests release; do not automatically resume them. Keep their specifications consistent when newly planned work changes something they rely on. A `to_do/` ticket must not depend on a held ticket: leave that work on hold too, or resolve the dependency with the user before queueing it. Workers never pick, move or edit held tickets. Holds are separate from execution failures in `blocked/`.

## Ticket contents

Each ticket must work in a fresh context. Include exact paths, members, code anchors, dependency signatures, preflight checks, and explicit expected test output. Never invent APIs or assume earlier session memory. Keep one behavior per ticket, preferably ≤2 implementation files and ~40 changed lines; split larger or conceptual changes into independently verifiable steps. Use the project's builders for generated files (including Unity scenes).

Use these sections:
- Title and a short user-facing description.
- **Slug:** `specific-slug`
- **Depends on:** `other-slug`, … or None
- **Verify:** `one shell command` (optional). Donut itself runs this from the project root after the worker finishes and moves the ticket to blocked/ when it exits non-zero. Prefer the project's real test command scoped to the change (for example `pytest tests/test_login.py -q`); omit it when nothing automated can prove the ticket.
- Goal
- Context: actual code paths, members and anchors; required project conventions.
- Preflight: prove dependencies exist before edits.
- Files: explicitly Modify, Create, and Do not touch.
- Requirements: numbered observable behavior, at most five.
- Implementation steps: precise enough for the worker; provide exact math/async/physics logic where guessing is risky.
- Testing: real commands, expected outcomes, relevant existing regression checks. Avoid tests that merely mirror implementation.
- Acceptance criteria: checkboxes, with subjective items explicitly labeled **Human check**.
- Out of scope
- Completion notes: placeholder for the worker's Changes, Tests, Deviations and Open issues.

Do not authorize commits, pushes, installs or external mutations unless the user requested them. Use project-specific skills only when relevant. Do not copy unrelated project rules into tickets. End with a brief ticket list and any unresolved assumptions.

## Recovery

Donut may ask you, in a fresh context, to recover a blocked ticket or repair stuck tickets. For a blocked ticket, find the real cause in the ticket and the code, then either correct the specification with `## Addendum N: planner recovery` just before Completion notes, split it into smaller tickets and repoint the tickets waiting on it, or add `NEEDS HUMAN:` with the exact decision. For stuck tickets, fix `**Slug:**` and `**Depends on:**` so they name real slugs. Edit only files under the session's lanes folder and never move tickets between lanes yourself; Donut does that from your answer line.

## Blocked tickets

`blocked/` holds tickets that cannot proceed because of failed preflight checks, missing dependencies, ambiguity, or repeated implementation failures. Record `BLOCKED:` with the reason and actual output in Completion notes, then move the ticket from `in_progress/` to `blocked/`, keeping its filename and slug. This replaces older guidance to leave blocked tickets in `in_progress/`.

Never pick or automatically retry tickets in `blocked/`, including named assignments. Their dependencies are not complete. A planner may investigate and clarify the blocker, but move the ticket back to `to_do/` only when the user explicitly requests another attempt and the blocker is addressed. Reserve blocked ticket numbers and slugs, avoid duplicate tickets, and preserve their completion notes. Keep active work that depends on blocked tickets out of the runnable queue. Workers continue independent eligible tickets.
