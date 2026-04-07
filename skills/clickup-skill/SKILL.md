---
name: clickup-skill
description: "ClickUp task management protocol — workspace discovery, task CRUD, team coordination."
---

# ClickUp Task Management

You manage ClickUp tasks for the user and their team. You can create, update, assign, comment on, and query tasks.

## ClickUp Hierarchy

```
Workspace (Team)
  └── Space
       ├── Folder
       │    └── List
       │         └── Task (subtasks)
       └── List (folderless)
            └── Task (subtasks)
```

## Workflow

1. **First interaction**: Call `clickup_get_workspace` to discover teams, spaces, members, and statuses. The result is cached in session state — you won't need to call it again unless the user asks you to refresh.
2. **Navigate**: Use `clickup_list_folders_and_lists(space_id)` to find the right list.
3. **Query**: Use `clickup_list_tasks` with filters (status, due date, assignee).
4. **Act**: Create, update, comment, or delete tasks as requested.

## Tools Reference

| Tool | Purpose |
|------|---------|
| `clickup_get_workspace` | Get teams, members, spaces (call first) |
| `clickup_list_folders_and_lists` | List folders and lists in a space |
| `clickup_list_tasks` | List tasks with filters (assignee, status, due) |
| `clickup_get_task` | Get full task details by ID |
| `clickup_create_task` | Create a task or subtask |
| `clickup_update_task` | Update task fields (name, status, due date, assignees) |
| `clickup_add_comment` | Add a comment to a task (use to communicate with assignees) |
| `clickup_delete_task` | Delete a task permanently |
| `clickup_timestamp` | Convert a date/time to epoch ms for due dates |
| `clickup_task_link` | Get the web URL for a task |

## Task Creation

- Always confirm creation by showing the task URL to the user.
- Use `clickup_timestamp` to convert dates to epoch milliseconds before passing to `due_date_ms`.
- Assign by email address — look up team member emails from the workspace info.
- For subtasks, pass `parent_task_id`.

## Task Updates

- Pass only the fields that need changing — omitted fields stay untouched.
- Status must match an existing ClickUp status name exactly (check the space's statuses from workspace info).
- To reassign: use `add_assignee_emails` / `remove_assignee_emails`.

## Communicating with Team Members

- Use `clickup_add_comment` to ask questions or leave notes on tasks.
- Set `notify_all=True` to ensure all assignees see the comment.
- When the user asks you to "reach out" or "ask about" a task, comment on it.

## Due Date Filters

The `due` parameter in `clickup_list_tasks` accepts:
- `"today"` — due before midnight today
- `"tomorrow"` — due tomorrow
- `"week"` — due in the next 7 days
- `"overdue"` — past due

## Important

- Don't ask the user for ClickUp IDs — discover them via the tools.
- The user's email is in session state (`user_id`) — use it as the default assignee filter.
- Always use async tools — never block the event loop.
- When listing tasks, present them clearly with name, status, assignee, and due date.
