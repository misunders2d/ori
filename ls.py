import os
def main():
    print(f"Current working directory: {os.getcwd()}")
    print("Listing files in root:")
    for item in os.listdir("/code"):
        print(item)
if __name__ == "__main__":
    main()
