import os
import shutil
import sqlite3
import json

def test_run_audit():
    audit = {}
    
    # 1. Who am I?
    audit["uid"] = os.getuid()
    audit["gid"] = os.getgid()
    
    # 2. Disk Space
    total, used, free = shutil.disk_usage(".")
    audit["disk"] = {
        "total_gb": total // (2**30),
        "used_gb": used // (2**30),
        "free_gb": free // (2**30),
        "percent": (used/total) * 100
    }
    
    # 3. Data Directory Permissions
    data_dir = "./data"
    audit["data_dir"] = {
        "exists": os.path.exists(data_dir),
        "mode": oct(os.stat(data_dir).st_mode) if os.path.exists(data_dir) else None,
        "writable": os.access(data_dir, os.W_OK)
    }
    
    # 4. DB File Status
    dbs = ["ori-sessions.db", "ori-scheduler.db", "pending_actions.db"]
    audit["dbs"] = {}
    for db in dbs:
        path = os.path.join(data_dir, db)
        if os.path.exists(path):
            try:
                s = os.stat(path)
                # Try a test write
                try:
                    # SQLite test write
                    db_test_path = path
                    with sqlite3.connect(db_test_path) as conn:
                        conn.execute("CREATE TABLE IF NOT EXISTS audit_test (id INTEGER PRIMARY KEY)")
                        conn.execute("INSERT INTO audit_test DEFAULT VALUES")
                        conn.execute("DROP TABLE audit_test")
                    test_write = "Success"
                except Exception as e:
                    test_write = f"Failed: {str(e)}"
                    
                audit["dbs"][db] = {
                    "exists": True,
                    "mode": oct(s.st_mode),
                    "uid": s.st_uid,
                    "gid": s.st_gid,
                    "test_write": test_write
                }
            except Exception as e:
                audit["dbs"][db] = {"error": str(e)}
        else:
            audit["dbs"][db] = {"exists": False}
            
    # Raise error to see the audit in the tool output
    raise AssertionError(json.dumps(audit, indent=2))
