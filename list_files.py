import os
import sys

def main():
    root = os.getcwd()
    print(f"Listing files in {root}")
    for item in os.listdir(root):
        print(item)

if __name__ == "__main__":
    main()
