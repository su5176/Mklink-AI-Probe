---
name: maintaining-mklink-ai-probe
description: Maintainer-only reference index for explicitly requested MKLink repository build, release, or handoff work. Not for installing or using MKLink to flash, debug, inspect symbols, or open the GUI.
---

# MKLink Maintainer References

Use only in the MKLink source checkout for repository maintenance explicitly
requested by the maintainer. The checkout is the directory containing
`AGENTS.md` and `scripts/ai_memory.py`, possibly below the Git root.

`AGENTS.md` is the single maintenance policy. If it is already loaded, do not
reread it. Routine fixes follow that policy without loading this Skill.

Read only the reference needed for the current task:

| Task | Reference from source root |
| --- | --- |
| Continue unfinished work | `docs/ai/CURRENT_HANDOFF.md` |
| Source development | `docs/ai/development.md` |
| Build storage and commands | `docs/ai/build-storage.md` |
| Desktop/sidecar/NSIS packaging | `skills/tauri-gui-builder/SKILL.md` |
| Explicitly authorized official publication | [releasing.md](references/releasing.md) |

Reading a publication procedure is not permission to publish. Do not install
this Skill or repository instructions into end-user MKLink installations.
