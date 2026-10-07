import json
import sqlite3

db = sqlite3.connect('sqlite.db')
cur = db.cursor()
cur.execute("SELECT rendered FROM deployments WHERE id='dep-35992684eea1'")
row = cur.fetchone()
if row:
    rendered = json.loads(row[0])
    for fname, content in rendered.items():
        print(f"File: {fname}")
        with open(fname, "w") as f:
            f.write(content)
else:
    print("Deployment not found.")
