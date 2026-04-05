
import os
def test_check_data():
    data_path = os.path.abspath("data")
    print(f"\nData path: {data_path}")
    if os.path.islink(data_path):
        print(f"Data is a SYMLINK to {os.readlink(data_path)}")
    else:
        print("Data is a real directory.")
    
    # Check if we can see files in it
    if os.path.exists(data_path):
        print(f"Contents: {os.listdir(data_path)}")
