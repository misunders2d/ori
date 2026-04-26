import importlib.metadata

for d in importlib.metadata.distributions():
    if d.metadata['Name'].startswith('google'):
        print(d.metadata['Name'], d.version)
