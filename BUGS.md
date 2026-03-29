# 🐛 Bug List

1.  **Delayed Credential Check:** `evolution_commit_and_push` only checks for `GITHUB_TOKEN` and `GITHUB_REPO` after full sandbox verification, causing long wait times before notifying the user about missing authentication. *Status: Open*
