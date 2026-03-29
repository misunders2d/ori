from google.adk.events.event import Event
from google.genai import types

def inspect():
    # Create a mock function call
    fc = types.FunctionCall(name="test_tool", args={"arg1": "val1", "summary": "my summary"}, id="123")
    # See how to get args
    print(f"FC name: {fc.name}")
    print(f"FC args: {fc.args}")
    print(f"FC id: {fc.id}")

if __name__ == "__main__":
    inspect()
