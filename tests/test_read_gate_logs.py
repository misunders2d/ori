import os
def test_read_gate_logs():
    log_paths = ["data/agent.log", "data/agent.log.1"]
    for path in log_paths:
        if os.path.exists(path):
            print(f"\n--- LOG: {path} ---")
            with open(path, "r") as f:
                content = f.readlines()
                for line in content[-200:]: # last 200 lines
                    if "Gate:" in line or "Perimeter Locked" in line or "authorized" in line:
                        print(line.strip())
    assert True
