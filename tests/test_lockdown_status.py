import os
def test_lockdown_status():
    log_content = ""
    for path in ["data/agent.log", "data/agent.log.1"]:
        if os.path.exists(path):
            with open(path, "r") as f:
                log_content += f.read()
    
    has_signal = "PERIMETER LOCKDOWN" in log_content
    has_gate = "Gate Config: Perimeter Locked" in log_content
    has_decision = "Gate:" in log_content
    
    raise AssertionError(f"Signal: {has_signal} | Active: {has_gate} | Decisions: {has_decision}")
