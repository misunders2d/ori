import os

def search():
    matches = []
    log_paths = ["data/agent.log", "data/agent.log.1"]
    for path in log_paths:
        if os.path.exists(path):
            with open(path, "r") as f:
                for line in f:
                    if "Gate:" in line:
                        matches.append(line.strip())
    return matches

if __name__ == "__main__":
    for m in search():
        print(m)
