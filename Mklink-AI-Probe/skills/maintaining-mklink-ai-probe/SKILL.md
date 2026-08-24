---
name: maintaining-mklink-ai-probe
description: Maintainer-only workflow for authorized Mklink AI Probe repository development, review, build, handoff, and release work. Invoke explicitly in a source checkout; never use for end-user flashing, hardware debugging, RTT, memory, serial, Modbus, or ordinary $mklink-ai-probe requests. Official publication still requires explicit maintainer authorization.
---

# Maintain Mklink AI Probe

This repository skill is the shared workflow. Do not depend on similarly named
skills installed on one developer's computer.

## Start From Live State

1. Resolve the project source root before reading relative paths. Start with
   the Git top-level directory. If it does not contain `AGENTS.md`,
   `docs/ai/project-memory.json`, `scripts/ai_memory.py`, and
   `skills/maintaining-mklink-ai-probe/SKILL.md`, inspect its direct child
   directories and select the only directory containing all four markers. Do
   not assume the current working directory or Git top-level is the source
   root; stop and inspect if no unique candidate exists.
2. Treat `AGENTS.md`, `docs/`, `scripts/`, and `skills/` paths below as
   relative to the resolved source root. Treat `release/` and `.worktrees/` as
   relative to the Git/workspace root unless the live repository says
   otherwise.
3. Read `AGENTS.md`, `docs/ai/CURRENT_HANDOFF.md`, and
   `docs/ai/project-memory.json` from the resolved source root.
4. From the resolved source root, run `python scripts/ai_memory.py validate`.
   Run `git status --short --branch` and `git log -12 --oneline` in the same
   repository.
5. Reconcile recorded state with Git and the running system. Correct stale
   memory instead of repeating completed work.
6. Check `git worktree list`. Reuse the current isolated worktree when one
   already exists; create another only when isolation is useful and authorized.

## Develop On A Branch

1. Before editing a runtime or user-facing feature or bug fix, start from a
   clean, current `master` and create a dedicated `feature/<topic>` or
   `fix/<topic>` branch. A separate worktree remains optional; the branch does
   not.
2. Never develop or commit feature and bug-fix work directly on `master`.
   Documentation-only maintenance and release handoffs are exempt unless the
   maintainer requests a branch.
3. Keep the implementation, regression coverage, final verification, real-
   hardware evidence, and project-memory update on the feature or fix branch.
4. Before merging, incorporate the current `master` into the branch when it has
   advanced, then rerun the full final gate. Treat any code change or conflict
   resolution after verification as invalidating the earlier evidence.
5. Merge into `master` only after the branch passes its required automated and
   real-surface gates. After merging, verify that `master` contains the tested
   branch tip, project memory validates, and the worktree is clean. Push only
   when authorized.

## Turn Requests Into Results

1. Identify the observed problem, the developer's intended outcome, affected
   users, constraints, and a concrete success signal.
2. Inspect the relevant code, tests, logs, UI, hardware state, and existing
   patterns before choosing a solution.
3. Before changing a product, interaction, architecture, workflow, or testing
   strategy, present the observed problem, viable options, tradeoffs,
   recommended choice, and concrete acceptance criteria. Wait for explicit
   maintainer confirmation before implementation. Once the strategy is
   confirmed, root-cause fixes that preserve it may proceed without asking
   again.
4. Ask only when another unanswered choice would materially change the result
   or require new authority. Otherwise state the smallest reversible
   assumption and continue.
5. Find the root cause. Keep the change within the owning module and avoid
   speculative abstractions or unrelated cleanup.
6. Use a short written plan for multi-step, risky, or cross-module work. Small
   fixes can proceed without a long plan, but still require a fix branch. Update
   the plan when evidence changes it.
7. Add regression coverage when it is useful and economical. Do not require a
   separate RED commit, a test-first ceremony, or broad tests for a narrow edit.
8. Prefer repository scripts and established APIs. Keep runtime dependencies
   bundled when users should not need a local toolchain.

## Project Invariants

- Default ELF/DWARF handling is bundled `pyelftools`. Invoke local
  `readelf`/`addr2line` only when the user explicitly selects the external
  backend.
- HPM targets use the dedicated ROM API and never load FLM.
- Generate only standard NSIS by default. MSI and WebView2-offline bundles need
  explicit user authorization.
- Put installer artifacts in the workspace-level `release/<build-time>/`
  directory and keep them out of Git.
- Prefer real Edge/Playwright/WebView2, computer use, and real hardware for
  behavior that depends on them; use focused automated tests for fast feedback.
- Never commit firmware, Packs, FLM files, logs, screenshots, full probe IDs,
  COM numbers, usernames, credentials, signing keys, or local hardware paths.
- Preserve user changes in a dirty worktree. Never discard unrelated work.

## Verify And Hand Off

1. For every runtime or user-facing feature and bug fix, run the full Python
   and GUI suites plus the production build on its branch before merge.
   Proportional focused tests remain useful for iteration but do not replace
   this final gate.
2. Complete a real-hardware closed loop on the affected Web, Tauri, or device
   workflow before merge and release. Mocked/component-only verification is
   insufficient; if the required hardware surface is unavailable, obtain an
   explicit maintainer waiver and record the exception.
3. Run `git diff --check` and record what was validated on the real surface.
4. Update `docs/ai/project-memory.json` with current facts, decisions, evidence,
   limits, and next actions. Run `python scripts/ai_memory.py render` and
   `python scripts/ai_memory.py validate`.
5. Commit and push only when authorized. Finish with a clean worktree unless
   the user explicitly leaves work in progress.

## Official Releases

Do not infer permission to sign, tag, publish, update `updates/latest.json`, or
sync Gitee. Contributors may prepare and verify release candidates, but only the
maintainer's computer or maintainer-controlled CI may publish an official
release. When publication is explicitly requested, follow
`references/releasing.md` exactly.
