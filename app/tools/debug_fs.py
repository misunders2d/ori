import os
import stat
import json

def get_fs_info(path):
    try:
        s = os.stat(path)
        return {
            "path": path,
            "mode": stat.filemode(s.st_mode),
            "uid": s.st_uid,
            "gid": s.st_gid,
            "is_dir": os.path.isdir(path),
            "writable": os.access(path, os.W_OK)
        }
    except Exception as e:
        return {"path": path, "error": str(e)}

async def audit_permissions() -> str:
    targets = ["./data", "./data/ori-sessions.db", "./data/ori-scheduler.db", "./data/agent.log"]
    results = [get_fs_info(t) for t in targets]
    
    # Also check who we are
    uid = os.getuid()
    gid = os.getgid()
    
    data = {
        "process_uid": uid,
        "process_gid": gid,
        "results": results
    }
    
    with open("data/fs_audit.json", "w") as f:
        json.dump(data, f, indent=2)
        
    return f"FS Audit complete. Process UID: {uid}. Writable data dir: {data['results'][0]['writable']}"
