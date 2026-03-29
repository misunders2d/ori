# 🧬 Ori Development Roadmap

This document is the official blueprint for Ori's growth. It serves as the source of truth for all self-evolution tasks.

## 🔭 Roadmap (Vision)
*High-level, long-term strategic goals. New items require user approval.*

1. **A2A Protocol (Ori-Net):** Implement the native Google ADK A2A protocol to enable real-time, decentralized technical collaboration between independent Ori instances. Use Agent Cards for discovery and strictly isolate human-memory from technical DNA exchange. *Status: Phase 1 (In Progress)*

## 🛠️ Backlog (Technical Debt & Bugs)
*Practical technical improvements and known bug fixes.*

1. **Delayed Credential Check** - `evolution_commit_and_push` only checks for `GITHUB_TOKEN` and `GITHUB_REPO` after full sandbox verification, causing long wait times before notifying the user about missing authentication. *Status: Open*

## 📜 Principles of Evolution
*   **Source of Truth:** This file is the primary reference for all self-evolution decisions.
*   **Permission First:** Suggestions for the Roadmap are welcome, but additions require explicit user approval.
*   **Continuous Alignment:** Every code update must be checked against this roadmap to prevent technical drift.
