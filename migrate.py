import psycopg2

conn = psycopg2.connect(
    'postgresql://voiceguard_admin:Newcafe123!@voiceguard-db.postgres.database.azure.com/postgres',
    sslmode='require'
)
c = conn.cursor()

alterations = [
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS caller_id TEXT",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS agent_qos_tx TEXT DEFAULT 'Good'",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS agent_qos_rx TEXT DEFAULT 'Good'",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS customer_qos_tx TEXT DEFAULT 'Good'",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS customer_qos_rx TEXT DEFAULT 'Good'",
]

for sql in alterations:
    print(f"Running: {sql}")
    c.execute(sql)

conn.commit()
conn.close()
print("✅ All columns added successfully!")
