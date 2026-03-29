import os
def test_list_scripts():
    path = "/code/scripts"
    if os.path.exists(path):
        files = sorted(os.listdir(path))
        assert False, "\n".join(files)
    else:
        assert False, f"{path} does not exist"
