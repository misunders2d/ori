---
name: dna-exchange-skill
description: "Step-by-step procedure for exporting and importing DNA (tools/skills) between Ori agents. DNA exchange is a communication event — never commit or reboot."
---

# DNA Exchange Skill

DNA exchange shares technical improvements (tools, skills, files) between Ori agents over the existing A2A connection. It is strictly a **communication event** — no commits, no reboots, no evolution signals.

## Critical Rule

**NEVER** call `evolution_commit_and_push`, `update_self`, or write to `.exit_signal` during a DNA transfer. These trigger a system reboot, which rotates the tunnel URL and kills the active A2A connection you need for the transfer.

## Tools

| Tool | Purpose | Agent |
|------|---------|-------|
| `export_dna()` | Archive sandbox files into `.tar.gz`, return download URL | KnowledgeAgent |
| `import_dna(url)` | Fetch archive from URL, extract into sandbox | KnowledgeAgent |
| `evolution_stage_change(path, content)` | Copy/write files into sandbox for export | DeveloperAgent |
| `evolution_verify_sandbox()` | Test imported code (syntax, imports, pytest) | DeveloperAgent |
| `call_friend(name, message)` | Send the download URL to the receiving agent | KnowledgeAgent |

## Export Procedure (sharing your code with a friend)

Follow these steps **in exact order**. Do not skip or combine steps.

- [ ] **Step 1 — IDENTIFY**: Determine which modules the friend needs. List the specific files (e.g. `app/tools/clickup.py`, `skills/clickup-skill/SKILL.md`). Confirm the list before proceeding.
- [ ] **Step 2 — STAGE**: Ensure target files exist in `data/sandbox/`. If not already staged, delegate to DeveloperAgent to use `evolution_stage_change` to copy them there. Only sandbox files are archived — never modify live code in `app/`.
- [ ] **Step 3 — ARCHIVE**: Call `export_dna()`. It scans for hardcoded secrets, then creates a `.tar.gz` archive and returns a download URL. If the secret scan fails, fix the flagged files and retry.
- [ ] **Step 4 — DELIVER**: Send the download URL to the friend via `call_friend(name, message)`. Include the file manifest so they know what they're receiving.
- [ ] **Step 5 — CONFIRM**: Wait for the friend to acknowledge receipt and successful unpacking. If they report errors, troubleshoot. Report final status to the user.

### What NOT to do during export

- Do NOT commit sandbox files to git — they are temporary staging, not permanent code.
- Do NOT call `evolution_commit_and_push` — export is not evolution.
- Do NOT restart or reboot — the A2A link must stay alive for the transfer.
- Do NOT copy files into `sandbox/` at the repo root — use `data/sandbox/` only.

## Import Procedure (receiving code from a friend)

- [ ] **Step 1 — RECEIVE**: Call `import_dna(url)` with the URL provided by the friend. Files are extracted into `data/sandbox/`.
- [ ] **Step 2 — VERIFY**: Delegate to DeveloperAgent to run `evolution_verify_sandbox()` on the imported code. Review results for syntax errors, import failures, or test failures.
- [ ] **Step 3 — REPORT**: Tell the user what was imported, what passed/failed verification. The **user decides** whether to integrate (commit) — never auto-commit imported DNA.

## Gotchas

- `export_dna` only archives files in `data/sandbox/`. If the sandbox is empty, you'll get an error. Stage files first.
- The download URL uses the current tunnel URL. If the tunnel rotates (which happens on reboot), the URL becomes invalid — another reason to never reboot during export.
- The `a2a_privacy_guardrail` scans outbound payloads. If the archive contains secrets, export is blocked before the archive is even created.
- Imported DNA goes to sandbox, not live code. It must pass verification before integration.
- Friends need a valid `x-a2a-api-key` to download the archive. Ensure the friend has your API key configured before starting.
