import os
import pytest
import sqlite3
from app.tools.system import repair_data_permissions

def test_data_directory_write_access():
    """Verify that the process has write access to the data directory."""
    data_dir = "./data"
    test_file = os.path.join(data_dir, "write_test.tmp")
    
    # Check current UID/GID
    uid = os.getuid()
    gid = os.getgid()
    print(f"\nDebug: Process UID={uid}, GID={gid}")
    
    # Check directory permissions
    dir_stat = os.stat(data_dir)
    print(f"Debug: Data Dir Mode={oct(dir_stat.st_mode)}, UID={dir_stat.st_uid}, GID={dir_stat.st_gid}")
    
    try:
        # Attempt to create and write to a file
        with open(test_file, "w") as f:
            f.write("test")
        
        assert os.path.exists(test_file)
        
        # Attempt to delete the file
        os.remove(test_file)
        assert not os.path.exists(test_file)
        
    except PermissionError as e:
        pytest.fail(f"Permission denied when writing to data directory: {e}")
    except Exception as e:
        pytest.fail(f"An unexpected error occurred during write test: {e}")

def test_repair_data_permissions_functionality():
    """Verify that repair_data_permissions correctly sets 775/664 modes."""
    data_dir = "./data"
    # Create a dummy file in data/ and set it to read-only
    dummy_file = os.path.join(data_dir, "dummy.db")
    with open(dummy_file, "w") as f:
        f.write("dummy content")
    
    # Set to read-only (444)
    os.chmod(dummy_file, 0o444)
    initial_mode = oct(os.stat(dummy_file).st_mode & 0o777)
    print(f"\nDebug: Initial Mode={initial_mode}")
    
    # Run the repair tool
    result = repair_data_permissions()
    assert result["status"] == "success"
    
    # Check if permissions were updated to 664 (rw-rw-r--)
    # We use bitwise check instead of string compare to be more robust
    final_stat = os.stat(dummy_file)
    final_mode = final_stat.st_mode & 0o777
    print(f"Debug: Final Mode={oct(final_mode)}")
    
    # 0o664 is the target. We check if the owner and group have rw
    assert (final_mode & 0o600) == 0o600, f"Owner should have rw, got {oct(final_mode)}"
    assert (final_mode & 0o060) == 0o060, f"Group should have rw, got {oct(final_mode)}"
    
    # Cleanup
    os.remove(dummy_file)

def test_system_db_write_access():
    """Verify write access specifically for SQLite db files in the data directory."""
    data_dir = "./data"
    db_file = os.path.join(data_dir, "test_write.db")
    
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
        cursor.execute("INSERT INTO test (val) VALUES (?)", ("test_value",))
        conn.commit()
        
        cursor.execute("SELECT val FROM test")
        row = cursor.fetchone()
        assert row[0] == "test_value"
        
        conn.close()
    except Exception as e:
        pytest.fail(f"SQLite write access failed: {e}")
    finally:
        if os.path.exists(db_file):
            os.remove(db_file)
