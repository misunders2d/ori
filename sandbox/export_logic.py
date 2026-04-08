import os
import zipfile

# Zip the files in the sandbox/ directory
def export_dna():
    target = "./tmp/exports/sergey_library.zip"
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file in ['clickup.py', 'pinecone_tools.py', 'graph_tools.py']:
            zipf.write(file, arcname=file)
    return target
export_dna()
