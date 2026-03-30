import os
def clean():
    for f in ["list_files.py", "clean_sandbox.py"]:
        if os.path.exists(os.path.join("./data/sandbox", f)):
            os.remove(os.path.join("./data/sandbox", f))

clean()
