import os
import stat
import pytest

@pytest.mark.infra
def test():
    data_dir = "./data"
    dummy_file = os.path.join(data_dir, "dummy.db")
    with open(dummy_file, "w") as f:
        f.write("dummy content")
    
    # Set to read-only (444)
    os.chmod(dummy_file, 0o444)
    print(f"Initial: {oct(os.stat(dummy_file).st_mode & 0o777)}")
    
    # Try to set 666
    os.chmod(dummy_file, 0o666)
    final_mode = os.stat(dummy_file).st_mode & 0o777
    print(f"Final: {oct(final_mode)}")
    
    # Check if 666 worked
    print(f"Owner Read: {bool(final_mode & 0o400)}")
    print(f"Owner Write: {bool(final_mode & 0o200)}")
    print(f"Group Read: {bool(final_mode & 0o040)}")
    print(f"Group Write: {bool(final_mode & 0o020)}")
    print(f"Other Read: {bool(final_mode & 0o004)}")
    print(f"Other Write: {bool(final_mode & 0o002)}")

if __name__ == "__main__":
    test()
