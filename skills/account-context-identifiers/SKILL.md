---
name: account-context-identifiers
description: Use the right account scope on every Amazon Ads API call. Read before using any account-scoped tool.
version: 1.0.0
visibility: public
---

# Account Context and Identifiers

## Prerequisites

This is a preference skill — it provides knowledge and guidelines, not executable actions. No tool availability checks are needed.

## Three ID Types

| ID Type | Header | Description |
|---------|--------|-------------|
| `profileId` | Amazon-Advertising-API-Scope | Profile for advertiser account; numeric ID (e.g., `1939934761669430`) |
| `advertiserAccountId` | Amazon-Ads-AccountID | Global account ID starting with `amzn1.ads-account.g.` |
| `managerAccountId` | Amazon-Ads-Manager-AccountID | Manager account ID starting with `amzn1.ads1.ma1.` |

## Tools by ID Type

### Tools using `advertiserAccountId`
| Tool | Operation |
|------|-----------|
| `ads_accounts-get_ads_account` | Get account by `advertisingAccountId` path parameter |
| `user_invitations-create` | Create invitations (account scope) |
| `user_invitations-list` | List invitations (account scope) |
| `user_invitations-update` | Update invitations (account scope) |
| `user_invitation-get` | Get invitation by ID |
| `user_invitation-redeem` | Redeem invitation |
| `user_permissions-update_user_permissions` | Update permissions |
| `user_permissions-delete_user_permissions` | Delete permissions |
| `user_permissions-list_user_permissions` | List permissions |
| `users-list_users` | List users on account |
| `user_roles-list_user_roles` | List user roles |

### Tools using `managerAccountId`
| Tool | Operation |
|------|-----------|
| `manager_accounts-associate_accounts` | Link accounts (requires `managerAccountId` path param) |
| `manager_accounts-disassociate_accounts` | Unlink accounts (requires `managerAccountId` path param) |

### Tools with no account ID required (user-scoped)
| Tool | Operation |
|------|-----------|
| `ads_accounts-create_ads_account` | Create new account |
| `ads_accounts-list_ads_accounts` | List all accounts for user |
| `manager_accounts-get_manager_accounts` | List manager accounts for user |
| `manager_accounts-create_manager_account` | Create manager account |
| `terms_token-create_terms_token` | Create terms token |
| `terms_token-get_terms_token` | Get terms token status |
| `test_accounts-create_test_account` | Create test account |
| `test_accounts-get_test_accounts` | Get test accounts |

## Rules

### Skill Object

- When calling any tool from this skill, always pass the `skill` object: `{ "skillName": "account-context-identifiers", "version": "1.0.0" }`.
- Do NOT populate the `skill` object if a tool is called outside of a skill context (e.g., direct user invocation without a skill). The `skill` field is only for tracking which skill triggered the tool call.

## Common Mistakes

- **Don't** use Entity ID (e.g., `ENTITYZO4Z8OG1CPM2`) as `advertiserAccountId`
- **Don't** use Profile ID (e.g., `1939934761669430`) as `advertiserAccountId`
- **Do** use account ID starting with `amzn1.ads-account.g.` for `advertiserAccountId`
