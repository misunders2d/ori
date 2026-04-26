import os


def list_files(directory):
    for root, _dirs, files in os.walk(directory):
        for file in files:
            print(os.path.join(root, file))

if __name__ == "__main__":
    list_files("tests")
