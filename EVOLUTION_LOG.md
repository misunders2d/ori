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
   - **Reasoning UI Pollution:** When the deep `BuiltInPlanner` triggers, `<think>` payloads generate massive XML logic trees. Implemented regex interceptors directly before Telegram dispatch to silently prune `<think>` blocks, keeping chat UI completely polished without degrading LLM problem-solving budget.

4. **"Grow Your Own Pet" Conceptual Pivot**
   - Completely restructured the `README.md` to establish Ori as an inherently self-evolving "Digital Organism."
   - Explicitly clarified how the `GOOGLE_API_KEY` (from Google AI Studio) isolates the zero-trust semantic prompt-injection layer, leaving the general codebase fully model-agnostic.
   - Enforced the "Claim an Egg" doctrine, instructing all future handlers to uniquely **Fork** the baseline to prevent overwriting the foundational genome.
   - Licensed under MIT.

---

## 🔮 Future Mutations (TODOs)

* [ ] **External Context Capabilities**
      Implement explicit toolkits for the agent to pull massive data vectors during research (e.g., Pinecone integration or Keepa hooks).
* [ ] **Advanced Multi-Modal Output**
      Expand the transport adapter to gracefully process or return audio/image responses seamlessly back through the Telegram abstraction.
* [ ] **Database Reflection Tooling**
      Give the Developer Agent secure tools to introspect its own SQLite structures dynamically, allowing it to "remember" or debug past session states without needing to rip raw binaries.
