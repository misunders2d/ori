import os
print("Sandbox contents:", os.listdir("."))
if os.path.exists("skills"):
    print("Sandbox skills:", os.listdir("skills"))
