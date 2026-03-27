# 🧬 Ori Evolution Log

This document serves as the long-term memory and tracked evolutionary state of Ori. It logs critical architectural shifts, system hardening iterations, and future DNA mutations (TODOs). By committing this file to the repository, Ori natively maintains an understanding of its own developmental history.

---

## 📅 March 27, 2026: The "Digital Pet" Refactoring & Hardening Sprint

### 🛠 Core Advancements
1. **Zero-Delta Update Hardening (`deploy.sh`)**
   - **Context:** The Host Supervisor historically ran reckless `git reset --hard origin/master` scripts during container updates, which wiped unpushed local changes and isolated branches.
   - **Evolution:** Implemented sophisticated topology logic (`git merge-base`) and working-tree integrity checks. Ori can now organically grow on isolated forks completely independent from GitHub remotes without nuking local commits.

2. **Telegram Poller Resilience (`telegram_poller.py`)**
   - **Sliding Buffer for Albums:** Multi-photo album uploads from Telegram were registering as individual concurrent messages, crashing executing agents mid-flight. Implemented a 1.5s sliding window buffer to natively group elements into unified payloads.
   - **4000-Character Constraint:** Extremely verbose reasoning payloads were breaking Telegram limits. Built recursive chunking logic to securely stream massive text responses indefinitely without breaking line bounds.
   - **Upstream Session Gating:** Disconnected the Telegram view layer from the `ADMIN_USER_IDS` sudo-escalation gate. Introduced an explicit `ALLOWED_USER_IDS` dynamic whitelist. Hardened it natively so you can grant access not just to specific users, but entire Group Chats (via `session_id`), dropping zero-trust interlopers instantly with explicit ID print-backs for fast approvals.

3. **ADK Abstraction Polish (`agent_executor.py`)**
   - **Dynamic Tool Surfaces:** Upgraded the extremely generic "The agent wants to execute *an action*" confirmation block. The pipeline now deeply parses the ADK dictionaries to explicitly print the raw function name (e.g., `system_update_self`).
  
4. **"Grow Your Own Pet" Conceptual Pivot**
   - Completely restructured the `README.md` to establish Ori as an inherently self-evolving "Digital Organism."
   - Explicitly clarified how the `GOOGLE_API_KEY` (from Google AI Studio) isolates the zero-trust semantic prompt-injection layer, leaving the general codebase fully model-agnostic.
   - Enforced the "Claim an Egg" doctrine, instructing all future handlers to uniquely **Fork** the baseline to prevent overwriting the foundational genome.
   - Licensed under MIT.

---

## 🔮 Endless Potential Evolutions

Every Ori instance is unique. The following are just a few directions your organism could grow, depending on what you teach it:

- **External Context Capabilities** — Connect Ori to vector databases like Pinecone, product APIs like Keepa, or any domain-specific data source to give it specialized knowledge.
- **Rich Media Responses** *(Evolved — March 27, 2026)* — The transport layer now natively supports sending images, audio, video, and documents back through any messenger adapter.
- **Database Self-Reflection** — Give the Developer Agent tools to introspect its own SQLite session structures, allowing it to debug or recall past interactions autonomously.
- **New Messenger Habitats** — Wire up Discord, Slack, WhatsApp, or a custom web UI. The `TransportAdapter` ABC makes this a single-file implementation.
- **Specialized Sub-Agents** — Breed new sub-agents for finance, DevOps, content creation, or anything else. Ori's coordinator will learn to delegate.

_The only limit is what you ask it to become._
