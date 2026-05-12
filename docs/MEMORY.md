# Memory subsystem

How Ori stores long-term knowledge in Neo4j and what the tool-level gates do before a write lands.

> Source: `app/tools/memory_tools.py`. Workflow / namespace / relationship reference: `skills/knowledge-graph-skill/SKILL.md`. Owned by `AmazonMemoryAgent` (`app/sub_agents/amazon_memory_agent.py`).

---

## 1. One store: Neo4j

Every memory, person, and entity is a node with a 1536-dim OpenAI embedding attached (`text-embedding-3-small`) plus typed relationship edges. No dual-write, no mirror.

Three node kinds:

| Label | Role |
|---|---|
| `:Memory` | Observations — meetings, decisions, incidents, procedures |
| `:Person` | Humans — team, clients, vendors, the user |
| `:Entity` | Referable things — brands, companies, products, locations |

Memories live in three namespaces (`personal`, `professional`, `technical`) with access tied to admin-status + `COMPANY_DOMAIN`. Details in the skill.

---

## 2. Pre-write gates (code-enforced)

`create_record` runs **three gates** in order before the write lands. The LLM cannot bypass any of them through prompt-side judgment — they're enforced in Python.

### Gate A — Identity gate

If the caller's `:Person` is still a bare auto-provisioned stub (no `first_name`), the tool returns `{"status": "needs_identity"}` and refuses to write. The agent's job is to ask for the user's name, run `update_person`, then retry.

**No bypass flag.** Anonymous memories accumulate as noise; preventing them is non-negotiable.

### Gate B — Duplicate gate (cosine ≥ 0.92)

`_find_semantic_duplicate_memory` vector-searches the target namespace for near-paraphrases of the proposed text. Top 3 above the threshold get surfaced as `{"status": "possible_duplicate", "matches": [...]}` and the create is refused.

Resolution path:
- Same knowledge as a match → `update_record(record_id, ...)` on the existing record. Don't duplicate.
- Different despite the similarity → retry with `force_create=true`. **Only after explicit user confirmation.**

Threshold: `_MEMORY_DUPLICATE_THRESHOLD = 0.92`. Tunable.

### Gate C — Relatives gate (0.75 ≤ cosine < 0.92, NEW 2026-05-12)

`_find_semantic_relatives_memory` surfaces topically-related (but not duplicate) prior records. Returns up to 5 matches as `{"status": "review_relatives", "matches": [...]}` and refuses to write.

This gate exists because of a regression caught 2026-05-12: the agent created a standalone "Today's Big Deals" exclusion record despite the user explicitly saying "as a followup" — the prior Prime Day 2026 deal record sat unlinked. The instruction-prompt fix was too fragile; the LLM ignored it. Code-level enforcement is the real fix.

Resolution path — pick ONE:

1. **Link** — retry with `related_memories=[{"memory_id": "<id>", "relation_type": "<verb>"}, ...]`. Typed verbs: `follows_up`, `corrects`, `supersedes`, `references`. Bare ID defaults to `RELATED_TO`.
2. **Standalone (confirmed)** — retry with `reviewed_relatives=True` and `related_memories=[]`. Explicit choice to create unlinked after seeing the matches.

Either path proves the agent saw the matches. **No bypass flag** to skip without doing one of these.

**Skip condition** — if the caller already passes a non-empty `related_memories` on the first call, the gate skips (caller pre-supplied IDs → search would duplicate work).

Threshold: `_MEMORY_RELATIVES_THRESHOLD = 0.75`. Tunable in tandem with the duplicate threshold.

### Fail-open posture

Both semantic gates are fail-open: if Neo4j or the OpenAI embedding service hiccups, the search helper logs a warning and returns an empty list. The write proceeds without the gate's quality nudge. This preserves write availability during infra issues — the gate is a quality check, not a safety gate. Identity gate stays load-bearing (it's about authorship integrity, not embeddings).

---

## 3. State machine

```
create_record(text, ..., force_create=False, reviewed_relatives=False, related_memories=None)
        │
        ▼
  needs_identity? ──── yes ───► return {status: "needs_identity"}; agent runs update_person, retries
        │
        no
        ▼
  force_create? ───── yes ──► skip dup gate
        │
        no
        ▼
  cosine ≥ 0.92? ──── yes ──► return {status: "possible_duplicate"};
        │                       agent either update_record OR retry force_create=True
        no
        ▼
  related_memories already supplied OR reviewed_relatives? ──── yes ──► skip relatives gate
        │
        no
        ▼
  0.75 ≤ cosine < 0.92? ─ yes ──► return {status: "review_relatives"};
        │                          agent either retry with related_memories=[...]
        no                          OR retry with reviewed_relatives=True, []
        ▼
  CREATE — embed, write node, wire relationship edges, return record_id
```

---

## 4. Tuning

| Constant | File:line | Default | When to tune |
|---|---|---|---|
| `_MEMORY_DUPLICATE_THRESHOLD` | `app/tools/memory_tools.py` | `0.92` | Lower if duplicates slip through; raise if false-positive matches block legitimate follow-up records |
| `_MEMORY_RELATIVES_THRESHOLD` | `app/tools/memory_tools.py` | `0.75` | Lower (e.g. 0.70) for stricter linkage requirement; raise (e.g. 0.80) if the gate fires too often on weakly-related records |
| `_MEMORY_RELATIVES_TOP_K` | `app/tools/memory_tools.py` | `5` | Raise if you want broader review surfaces; lower if 5 is overwhelming |

Tune in tandem — narrowing the duplicate band widens the relatives band. The two helpers query the same vector index with disjoint score filters, so there's no double-counting.

---

## 5. Caller surface

Only `AmazonMemoryAgent` reaches these tools directly. Coordinator and AmazonHeadAgent route memory work through it. Authorship is captured automatically as `author_user_id` on the created node — never pass an `author` argument.

For the user-facing operational rules (how to phrase requests, what trigger phrases imply linkage), the canonical reference is `skills/knowledge-graph-skill/SKILL.md`.
