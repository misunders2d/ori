import os
import pytest
import sqlite3


def test_data_directory_write_access():
    """Verify that the process has write access to the data directory."""
    data_dir = "./data"
    test_file = os.path.join(data_dir, "write_test.tmp")

    try:
        with open(test_file, "w") as f:
            f.write("test")

        assert os.path.exists(test_file)

        os.remove(test_file)
        assert not os.path.exists(test_file)

    except PermissionError as e:
        pytest.fail(f"Permission denied when writing to data directory: {e}")
    except Exception as e:
        pytest.fail(f"An unexpected error occurred during write test: {e}")

def test_system_db_wal_mode():
    """Verify that the database can be switched to WAL mode."""
    data_dir = "./data"
    db_file = os.path.join(data_dir, "test_wal.db")

    try:
        with sqlite3.connect(db_file) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")

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
        for ext in ["-wal", "-shm"]:
            if os.path.exists(db_file + ext):
                os.remove(db_file + ext)
