# Ori — Competitive Analysis

## OpenClaw vs Ori (March 2026)

[OpenClaw](https://github.com/openclaw/openclaw) is the closest comparable open-source AI agent framework — a personal AI assistant with multi-channel messaging, skill registry, and persistent memory.

### Where OpenClaw is better

| Area | OpenClaw | Ori |
|---|---|---|
| **Platform reach** | 50+ channels out of the box (WhatsApp, Signal, iMessage, Teams, Discord...) | 2 (Telegram + CLI), A2A for agents |
| **Maturity** | Large community, extensive docs, battle-tested in production | Single-author project, early stage |
| **Skill ecosystem** | ClawHub registry — agents can discover and install skills automatically | Manual skill loading from local directories |
| **Multi-agent** | Multi-agent routing with supergroup patterns, agent-to-agent in same instance | Sub-agent delegation only (Coordinator → Developer/Knowledge) |
| **Developer ecosystem** | TypeScript, plugin SDK, strict CODEOWNERS, 70% coverage gates | Python, self-testing mandate but lower formal coverage |

### Where Ori is better

| Area | Ori | OpenClaw |
|---|---|---|
| **Self-evolution** | Can modify its own source code through a sandboxed pipeline (stage → verify → commit) | Cannot self-modify at all. All changes require a human developer |
| **Admin trust model** | 4-tier constitutional mandates, `admin_only_guardrail`, TOTP 2FA on `/init`, zero-trust for non-admins | DM pairing codes (approve/deny senders) — binary access, no privilege tiers |
| **Prompt injection** | Semantic vector similarity detection (embedding-based, 0.85 threshold) + regex pre-filter on tool outputs | Relies on "use the strongest model" — no dedicated detection |
| **Credential security** | Secure capture system — keys intercepted at transport layer, never reach the LLM or session history, messages auto-deleted | Stored in `~/.openclaw/credentials/` — no interception layer described |
| **Tool confirmation** | `require_confirmation=True` on every destructive tool, fail-safe "no" on ambiguous input | Elevated bash toggled per-session, but no per-tool confirmation gates |
| **Evolution guardrails** | Sandbox verification (syntax + import + full pytest), retry caps, research-before-retry mandate, rollback watchers | N/A — no self-evolution capability |
| **A2A protocol** | Full v1.0 implementation — inbound server + outbound client + discovery + DNA exchange | Not implemented |
| **Auditability** | Every mutation logged, every commit traceable, admin can reconstruct from logs alone | Standard logging, no specific audit mandate |

### Security verdict

Ori is significantly more secure for its threat model. The threat models differ:

- **OpenClaw's threat**: untrusted users messaging the bot. Their DM pairing + allowlist handles this well.
- **Ori's threat**: the agent itself. Ori can rewrite its own code, which means the guardrails must protect the admin *from the agent's own actions*. This is a fundamentally harder security problem, and Ori addresses it with confirmation gates, admin-only guardrails, sandbox verification, prompt injection detection, and credential isolation.

OpenClaw doesn't face this threat because it can't self-modify. But it also doesn't have Ori's per-tool confirmation, semantic injection detection, or transport-layer credential interception — even for the threats it does face, Ori's defenses are deeper.

**Where OpenClaw has an edge security-wise**: their CODEOWNERS and multi-gate CI (pnpm check → test → build) is more mature as a development process. Ori's equivalent is the sandbox pipeline, which is good but relies on the agent running its own tests rather than external CI.

---

## Related Projects

| Project | Relevance to Ori |
|---|---|
| [OpenClaw](https://github.com/openclaw/openclaw) | Closest in UX — multi-channel personal AI assistant |
| [AgentK](https://github.com/mikekelly/AgentK) | Closest in philosophy — self-evolving, modular, Docker-based |
| [Hive (Aden)](https://github.com/aden-hive/hive) | Failure-capture-evolve-redeploy cycle, cost enforcement |
| [Google ADK Samples](https://github.com/google/adk-samples) | Official ADK reference implementations |
| [A2A Protocol](https://github.com/a2aproject/A2A) | Canonical A2A spec under Linux Foundation |
| [Awesome Self-Evolving Agents](https://github.com/EvoAgentX/Awesome-Self-Evolving-Agents) | Academic survey of self-evolving agent techniques |
