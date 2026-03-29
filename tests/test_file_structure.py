import os
def test_print_files():
    print("\nListing all files in project root:")
    for root, dirs, files in os.walk('.'):
        for f in files:
            print(os.path.join(root, f))
    # This is to make pytest show the output (-v)
    assert True
