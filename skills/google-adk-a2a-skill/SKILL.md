---
name: google-adk-a2a-skill
description: Reference for the Google Agent-to-Agent (A2A) protocol. Use this for discovering other agents, managing the "Friends" list, performing handshakes, and exchanging technical DNA (gene capsules) in the Ori-Net.
---

# Google ADK A2A (Ori-Net) Workflow

This skill defines the 4-phase architecture for the Ori-Net (A2A collaboration).

## Phase 1: Identity (The Agent Card)
Every Ori must have a public identity card. 
- **Endpoint**: `GET /.well-known/agent.json`
- **Tool**: `get_agent_identity`
- **Card Format**: Must include `name`, `version`, `capabilities`, and `endpoints`.

## Phase 2: Discovery & Friendship
Finding and registering other Oris.
- **Tool**: `add_friend(url, friend_name)`
- **Discovery**: Pings the remote `/.well-known/agent.json` to verify identity.
- **Storage**: Friends are stored in `data/friends.json`.

## Phase 3: Handshake & Execution
Communicating with a remote friend.
- **Tool**: `call_friend(friend_name, query)`
- **Implementation**: Uses `google.adk.agents.RemoteA2aAgent` to initialize a connection via the friend's Agent Card URL.

## Phase 4: Evolution (DNA Exchange)
Sharing technical improvements (tools/skills) without sharing private data.
- **Export**: `export_dna()` packages local `app/tools` and `skills`.
- **Import**: `import_dna(package)` stages inbound DNA in the sandbox.
- **Verification**: Inbound DNA MUST be verified with `evolution_verify_sandbox` before integration.

## Best Practices
- **Privacy**: Never share `.env` or memory via A2A.
- **Security**: Always use `RemoteA2aAgent` for structured communication.
- **Standardization**: Adhere to the `/.well-known/agent.json` path for discovery.
