import os
def find_file(name, path):
    for root, dirs, files in os.walk(path):
        if name in files:
            return os.path.join(root, name)
    return None

print(f"Searching for .gitignore from /code...")
found = find_file('.gitignore', '/code')
if found:
    print(f"FOUND: {found}")
else:
    print("NOT FOUND")

print("\nFiles in root (detailed):")
for f in os.listdir('/code'):
    full_path = os.path.join('/code', f)
    is_dir = os.path.isdir(full_path)
    print(f" [{'DIR' if is_dir else 'FILE'}] {f}")
