import os


def search_files(directory, query):
    for root, _dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(('.py', '.md', '.txt', '.json', '.yml')):
                path = os.path.join(root, file)
                try:
                    with open(path, encoding='utf-8') as f:
                        if query in f.read():
                            print(f"Found in {path}")
                except Exception:
                    pass

if __name__ == "__main__":
    search_files(".", "Perform code evolution")
