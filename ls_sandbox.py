import os
def ls_r(p):
    for root, dirs, files in os.walk(p):
        for f in files:
            print(os.path.join(root, f))
ls_r('data/sandbox')
