---
name: github-skill
description: "A skill providing guidelines for interacting with GitHub repositories, code execution, and Git logic."
---

# GitHub Skill

This skill defines the operational logic for interacting with GitHub.
For operations involving repositories, cloning, or creating branches, use standard Git commands.

Use `GITHUB_TOKEN` to securely push to `GITHUB_REPO`. 
When the agent needs to evolve itself, push the changes back to its remote repository.
