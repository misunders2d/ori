"""Quick Neo4j connection test. Run: python test_neo4j.py"""

import os
import sys

# Load vault credentials into env
from deploy.vault import load_vault
load_vault()

uri = os.environ.get("NEO4J_URI", "")
user = os.environ.get("NEO4J_USERNAME", "")
password = os.environ.get("NEO4J_PASSWORD", "")

print(f"URI:      {uri}")
print(f"Username: {user}")
print(f"Password: {'*' * len(password)} ({len(password)} chars)")

if not uri or not password:
    print("\nERROR: NEO4J_URI or NEO4J_PASSWORD not set.")
    sys.exit(1)

from neo4j import GraphDatabase

driver = GraphDatabase.driver(uri, auth=(user, password))

try:
    driver.verify_connectivity()
    print("\nConnection: OK")
except Exception as e:
    print(f"\nConnection FAILED: {e}")
    sys.exit(1)

try:
    with driver.session() as session:
        result = session.run("RETURN 1 AS test")
        record = result.single()
        print(f"Query test: {record['test']} (expected 1)")
        print("\nAll good — Neo4j is working.")
except Exception as e:
    print(f"\nQuery FAILED: {e}")
    sys.exit(1)
finally:
    driver.close()
