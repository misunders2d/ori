import os
def test_list_files():
    files = sorted(os.listdir("/code"))
    assert False, "\n".join(files)
