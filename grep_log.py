import sys
with open('data/agent.log') as f:
    for line in f:
        if 'keepa' in line.lower() or 'error' in line.lower() or 'amazon' in line.lower():
            print(line.strip())
