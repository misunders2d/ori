from dataclasses import dataclass


@dataclass
class MockPayload:
    commit_message: str
    summary: str | None = None
    delete_files: list[str] | None = None

def extract(payload):
    clean_payload = {}
    if payload:
        try:
            if hasattr(payload, "model_dump"):
                clean_payload = payload.model_dump()
            elif hasattr(payload, "dict"):
                clean_payload = payload.dict()
            elif isinstance(payload, dict):
                clean_payload = payload
            else:
                clean_payload = {k: v for k, v in getattr(payload, "__dict__", {}).items() if not k.startswith("_")}
        except Exception as e:
            print(f"Error: {e}")
    return clean_payload

# Test with dataclass (similar to what some ADK versions might use)
p1 = MockPayload(commit_message="Hello", summary="Summary")
print(f"Dataclass: {extract(p1)}")

# Test with dict
p2 = {"commit_message": "Hello", "summary": "Summary"}
print(f"Dict: {extract(p2)}")

# Test with Pydantic-like object
class PydanticMock:
    def model_dump(self):
        return {"commit_message": "Hello", "summary": "Summary"}
p3 = PydanticMock()
print(f"Pydantic: {extract(p3)}")
