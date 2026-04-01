import os

def check():
    log_paths = ["data/agent.log", "data/agent.log.1"]
    for path in log_paths:
        if os.path.exists(path):
            with open(path, "r") as f:
                content = f.read()
                if "PERIMETER LOCKDOWN" in content:
                    return f"Found in {path}"
                if "Gate Config: Perimeter Locked" in content:
                    return f"Active in {path}"
    return "Not found"

if __name__ == "__main__":
    print(check())
