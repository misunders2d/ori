---
name: dna-exchange-skill
description: "Step-by-step procedure for exporting and importing DNA (tools/skills) between Ori agents over native A2A binary content. DNA exchange is a communication event — never commit or reboot during a transfer."
---

# DNA Exchange Skill

DNA exchange shares technical improvements (tools, skills, files)
between Ori agents over the A2A connection. It is strictly a
**communication event** — no commits, no reboots, no evolution signals.

ADK 2.0 redesign: the tarball rides **inline as binary `Content`** in
the A2A message, not as a downloadable URL. There is no public
download endpoint; the tunnel URL never appears in the chat.

## Critical Rule

**NEVER** call `evolution_commit_and_push`, `update_self`, or write to
`.exit_signal` during a DNA transfer. These trigger a system reboot,
which rotates the public tunnel URL and kills the active A2A
connection you need for the transfer.

## Tools

| Tool | Purpose | Agent |
|---|---|---|
| `export_dna(source_paths)` | Pack files into a `.tar.gz` (in memory + saved as artifact). Returns `tarball` (bytes), `manifest`, `artifact_id`. | KnowledgeAgent |
| `call_friend(name, content)` | Send a `types.Content` payload (text + binary parts) to a friend. | KnowledgeAgent |
| `import_dna(content_or_artifact_id_or_bytes)` | Receive bytes / artifact / Part. Extract into per-session sandbox. | KnowledgeAgent |
| `evolution_verify_sandbox()` | Test imported code (syntax, imports, pytest). | DeveloperAgent |

## Export procedure (sharing your code with a friend)

Steps **in exact order**:

- [ ] **1. IDENTIFY** — Determine which modules the friend needs. List
  specific project-relative paths (e.g. `app/tools/keepa.py`,
  `skills/keepa-skill/`). Directories are included recursively.
  Confirm the list with the user before proceeding.

- [ ] **2. ARCHIVE** — Call `export_dna(source_paths=[...])`. It reads
  directly from the project tree, scans for hardcoded secrets, packs a
  tar.gz in memory, and saves an audit copy as an artifact. Returns a
  dict with `tarball` (bytes), `manifest` (list of paths), and
  `artifact_id`.

- [ ] **3. DELIVER** — Build a `types.Content` carrying both the
  manifest as text and the tarball as binary, then call
  `call_friend(name, content)`:

  ```python
  from google.genai import types
  import json

  content = types.Content(
      role="user",
      parts=[
          types.Part.from_text(json.dumps({
              "kind": "dna",
              "manifest": result["manifest"],
              "size": result["size_bytes"],
          })),
          types.Part.from_bytes(
              result["tarball"],
              mime_type="application/gzip",
          ),
      ],
  )
  await call_friend(friend_name, content)
  ```

  The A2A SDK serializes the binary part as base64 `inline_data` per
  the A2A spec. Binary safety check (`BinaryContentScannerPlugin`)
  validates the receiver-side magic bytes + size cap.

- [ ] **4. CONFIRM** — Wait for the friend to acknowledge successful
  import + verification. If they report errors, troubleshoot. Report
  final status to the user.

### What NOT to do during export

- Do NOT call `evolution_commit_and_push` — export is not evolution.
- Do NOT restart or reboot — the A2A link must stay alive for the
  transfer (and rotating the tunnel URL invalidates the connection).
- Do NOT delegate to DeveloperAgent — KnowledgeAgent handles the
  entire export.

## Import procedure (receiving code from a friend)

- [ ] **1. RECEIVE** — When you observe a `Content` from a friend
  containing an `application/gzip` part with an accompanying text
  part marking it as DNA (`{"kind": "dna", ...}`), call
  `import_dna(part)` (or `import_dna(bytes)`). It extracts into
  `data/sandbox/<session_id>/` (per-session isolation).

- [ ] **2. VERIFY** — Delegate to DeveloperAgent to run
  `evolution_verify_sandbox()` on the imported code. Review results
  for syntax errors, import failures, or test failures.

- [ ] **3. REPORT** — Tell the user what was imported and what
  passed/failed verification. The **user decides** whether to commit;
  never auto-commit imported DNA.

## Backward-compat note

`import_dna` also accepts an `artifact_id` (string) — useful when the
sender already saved the tarball as an artifact and wants to refer to
it by id rather than transmit bytes again. This is supplementary; the
default flow is bytes inline.

## Gotchas

- `BinaryContentScannerPlugin` rejects parts above
  `ORI_A2A_MAX_BINARY_BYTES` (default 16 MiB). Large DNA bundles may
  need to be split or the cap raised explicitly.
- The `A2APrivacyPlugin` scans the outbound `call_friend` args for
  vault secret values. If the tarball happens to embed a hardcoded
  secret (it shouldn't — `export_dna` blocks that case at archive
  time), the call is rejected.
- Imported DNA goes to sandbox, not live code. It must pass
  verification before integration.
- Friends still need a valid `x-a2a-api-key` to call your A2A
  endpoint at all. Ensure the friend has your key configured (via
  `add_friend` + `update_friend_key`) before starting.
