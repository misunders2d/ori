---
name: clickup-integration
description: Full async ClickUp task management toolset for CRUD, discovery, and workspace management.
author: Bezos
created: 2026-04-08
verified: true
tags: [clickup, task-management, productivity, project-management, tools]
files:
  - app/tools/clickup.py
---

# clickup-integration

This evolution provides a comprehensive suite of tools for managing ClickUp tasks directly from the agent. It supports workspace discovery, task listing with filters, full CRUD (Create, Read, Update, Delete) for tasks and subtasks, and commenting.

## Usage

### Prerequisites

1.  Obtain a **ClickUp Personal API Token** from your ClickUp account settings (Apps > API Token).
2.  Store the token in your environment/vault:
    ```bash
    CLICKUP_API_TOKEN=pk_...
    ```

### Tools

- **`clickup_get_workspace`** — Discovers your teams, spaces, and member emails.
- **`clickup_list_tasks`** — Lists tasks with advanced filters (assignee, status, due date, list/folder).
- **`clickup_create_task`** — Creates new tasks or subtasks.
- **`clickup_update_task`** — Updates existing tasks.
- **`clickup_add_comment`** — Adds plain-text comments to tasks.
- **`clickup_delete_task`** — Deletes a task.

### Architecture

The tools use `httpx.AsyncClient` for all API calls and adhere to the ADK `ToolContext` pattern for session-based workspace caching.

## Files

- `app/tools/clickup.py`
