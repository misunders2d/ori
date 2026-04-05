import os

def search_files(directory, query):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        if query in f.read():
                            print(f"Found in {path}")
                except Exception:
                    pass

if __name__ == "__main__":
    search_files(".", "class AgentCard")
