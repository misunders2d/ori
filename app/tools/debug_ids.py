import os
import re

def find_ids():
    pattern = re.compile(r"Message from (.*?) \((tg_\d+)\)")
    ids = set()
    log_paths = ["data/agent.log", "data/agent.log.1"]
    for path in log_paths:
        if os.path.exists(path):
            with open(path, "r") as f:
                for line in f:
                    match = pattern.search(line)
                    if match:
                        ids.add(f"{match.group(1)}: {match.group(2)}")
    return list(ids)

if __name__ == "__main__":
    print(find_ids())
