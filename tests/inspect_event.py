from google.adk.events.event import Event
import inspect

def test_inspect_event():
    sig = inspect.signature(Event.__init__)
    print(f"Event signature: {sig}")
    # This should fail if my assumption is wrong
    # author is required according to the user's log
    assert 'author' in sig.parameters
