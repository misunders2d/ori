# `evolutions/` — DNA catalog

This directory holds **shareable self-evolution bundles** — small, self-contained code packages an Ori agent has produced and verified, ready to be sent to another Ori instance via A2A and re-applied locally.

See `docs/EVOLUTION.md` for the full evolution lifecycle (stage → verify → commit) and the tools that produce these bundles (`evolution_catalog`, `evolution_share`, `evolution_import`).

## Layout

```
evolutions/<slug>/
├── EVOLUTION.md       # Manifest (frontmatter + free-form description)
├── files/             # Staged source files, mirroring the project tree
├── tests/             # Accompanying pytest cases
└── manifest.json      # Structured fields (name, description, tags, file_paths)
```

A directory without `EVOLUTION.md` is treated as orphaned: `scripts/gen_docs.py` emits a stderr warning during every evolution-verify cycle so the gap doesn't go unnoticed. Orphans are still valid filesystem objects — the warning is to nudge whoever staged them to either complete the manifest or remove the directory.

## How to add a proposal

1. Sandbox-stage your changes the usual way (`evolution_stage_change`).
2. Run `evolution_verify_sandbox(check="pytest")` until green.
3. Use `evolution_catalog(name, description, file_paths, tags, usage_notes)` — this copies the sandbox into `evolutions/<slug>/` and writes the matching `EVOLUTION.md` + `manifest.json`.
4. To share, `evolution_share(slug)` packages the bundle for A2A delivery.

## Recovery / audit

Every stage / verify / commit / discard / rollback event is appended to `data/evolution_audit.jsonl` (one line per event, JSONL, append-only). When something looks off, `tail -n 200 data/evolution_audit.jsonl | jq` is the first place to look.
