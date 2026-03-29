import os
print(os.listdir("/code/"))
if os.path.exists("/code/app"):
    print("app/:", os.listdir("/code/app/"))
if os.path.exists("/code/scripts"):
    print("scripts/:", os.listdir("/code/scripts/"))
if os.path.exists("/code/data"):
    print("data/:", os.listdir("/code/data/"))
