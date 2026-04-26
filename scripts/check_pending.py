import os
import sqlite3
import sys

DB_PATH = os.path.abspath("./data/pending_actions.db")

def check_token(token):
    if not os.path.exists(DB_PATH):
        print("DB not found")
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT tool_name, expires_at FROM pending_actions WHERE token = ?", (token,))
    row = cursor.fetchone()
    if row:
        print(f"Token {token} found: {row[0]}, expires at {row[1]}")
    else:
        print(f"Token {token} not found")
    conn.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        check_token(sys.argv[1])
