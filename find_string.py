import os

def find_string(root_dir, search_string):
    for root, dirs, files in os.walk(root_dir):
        if ".git" in root or ".venv" in root or "__pycache__" in root:
            continue
        for file in files:
            file_path = os.path.join(root, file)
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    if search_string in f.read():
                        print(f"Found in: {file_path}")
            except Exception as e:
                pass

find_string("/code/", "I'm back!")
