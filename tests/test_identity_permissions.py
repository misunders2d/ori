import os
import pytest
import sqlite3
from app.tools.system import repair_data_permissions

def test_data_directory_write_access():
    """Verify that the process has write access to the data directory."""
    data_dir = "./data"
    test_file = os.path.join(data_dir, "write_test.tmp")
    
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
    """Verify that repair_data_permissions correctly sets modes."""
    data_dir = "./data"
    # Create a dummy file in data/ and set it to read-only
    dummy_file = os.path.join(data_dir, "dummy.db")
    with open(dummy_file, "w") as f:
        f.write("dummy content")
    
    # Run the repair tool
    result = repair_data_permissions()
    assert result["status"] == "success"
    
    # Check if permissions were updated
    final_stat = os.stat(dummy_file)
    final_mode = final_stat.st_mode & 0o777
    
    # Check for Owner/Group RW (at least)
    # In some sandboxed environments, 'others' write might be masked by umask
    assert (final_mode & 0o600) == 0o600, f"Owner should have rw, got {oct(final_mode)}"
    assert (final_mode & 0o060) == 0o060, f"Group should have rw, got {oct(final_mode)}"
    
    # Cleanup
    os.remove(dummy_file)

def test_system_db_wal_mode():
    """Verify that the database can be switched to WAL mode."""
    data_dir = "./data"
    db_file = os.path.join(data_dir, "test_wal.db")
    
    try:
        # Create a database and set it to WAL mode
        with sqlite3.connect(db_file) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
        
        # Verify it stuck
        with sqlite3.connect(db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode;")
            mode = cursor.fetchone()[0]
            assert mode.lower() == "wal"
            
    except Exception as e:
        pytest.fail(f"WAL mode enablement failed: {e}")
    finally:
        if os.path.exists(db_file):
            os.remove(db_file)
        # WAL mode creates extra files
        for ext in ["-wal", "-shm"]:
            if os.path.exists(db_file + ext):
                os.remove(db_file + ext)
