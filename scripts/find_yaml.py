import os
def find_yaml(startpath):
    for root, dirs, files in os.walk(startpath):
        for f in files:
            if f.endswith('.yaml') or f.endswith('.yml'):
                print(os.path.join(root, f))

if __name__ == "__main__":
    find_yaml('.')
