# Agent-to-UI Protocols (A2UI & AG-UI)

Enables agents to render rich, interactive, and streaming interfaces.

## AG-UI Components
First-class support in ADK for building chat UIs with:
- **Streaming**: Real-time token output.
- **State Sync**: Unified state between agent and frontend.
- **Agentic Actions**: Buttons and forms that trigger tool calls.

## A2UI Protocol
Used to generate structured UI elements from agent responses.

### Schema Example
```json
{
  "type": "dashboard",
  "components": [
    {
      "type": "chart",
      "style": "bar",
      "data": [10, 20, 30]
    }
  ]
}
```

## Implementation Strategy
1. **Define Schema**: Use the `google-adk` skill to bind structured Pydantic models to an agent's `output_schema`.
2. **Handle Interactivity**: Implement tool handlers that receive inputs from the UI.
