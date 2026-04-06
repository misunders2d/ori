import os
import shutil

PROJECT_ROOT = "/root/projects/bezos"
SANDBOX_DIR = "/root/projects/bezos/data/sandbox"

for folder in ["app/tools", "app/toolsets", "app/sub_agents", "tests"]:
    src_folder = os.path.join(PROJECT_ROOT, folder)
    dst_folder = os.path.join(SANDBOX_DIR, folder)
    if os.path.exists(src_folder) and os.path.exists(dst_folder):
        for item in os.listdir(src_folder):
            src_item = os.path.join(src_folder, item)
            dst_item = os.path.join(dst_folder, item)
            if not os.path.exists(dst_item):
                if os.path.isdir(src_item):
                    shutil.copytree(src_item, dst_item)
                else:
                    shutil.copy2(src_item, dst_item)
