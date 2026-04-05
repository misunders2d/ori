# 🗺️ Ori Development Roadmap

This document tracks the long-term vision, ongoing evolution, and pending tasks for Ori.

## 🔭 Roadmap (Vision)

The goal is to transform Ori from a capable assistant into a fully autonomous, secure, and collaborative digital organism.

### 🧬 Core Evolution & Intelligence
- **Model Hot Swap**: Enable the ability to switch between different LLM models (e.g., Gemini, GPT-4, Claude) dynamically without requiring a system reboot or code change.
- **Neural Expansion**: Integrate deeper RAG (Retrieval-Augmented Generation) for user preferences and technical history to handle massive context windows efficiently.

### 🛡️ Security & Reliability
- **Immune System Hardening**: Implement automated **Packages Vulnerability Checks** (e.g., `safety`, `pip-audit`) during the sandbox verification phase.
- **Rollback Refinement**: Improve the `trigger_rollback` mechanism to handle database migrations and state consistency across versions.

### 👥 Interaction & Multi-Tenancy
- **Enhanced Multi-User Interaction**: Move beyond simple chat isolation to a robust multi-user system with roles (Admin, Developer, User) and granular permissions.
- **Rich Interaction UI**: **Improve confirmation prompts** further by including code diffs (where possible) and more interactive elements in supported messengers.

### 🌐 Ori-Net (A2A)
- **Decentralized Collaboration**: Enable Oris to form "swarms" for parallelizing large research or coding tasks.
- **DNA Marketplace**: Create a secure way for Oris to discover and "purchase" (or exchange) verified skills and tools from each other.

---

## 📋 Backlog

### High Priority
- [ ] Implement `packages vulnerability check` tool.
- [ ] Refactor confirmation prompt logic to support multi-line diff summaries.
- [ ] Prototype model switching utility.

### Medium Priority
- [ ] Add support for Discord and Slack transport adapters.
- [ ] Implement automated documentation generation for new skills.

### Completed (Recent Milestones)
- [x] **v0.7.0**: Agent-to-Agent (A2A) Protocol implementation (Ori-Net).
- [x] **v0.7.1**: Robust Tool Confirmation system with human-readable summaries.
- [x] **Workflow Overhaul**: Enforced strict conversational routing and log-first diagnostic mandates.
