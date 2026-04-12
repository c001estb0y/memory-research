"""检查 session_summaries 与 observations 的关联关系"""
import sqlite3

conn = sqlite3.connect("../SourceMem/dpar-mem/dpar-hughesli.db")
conn.row_factory = sqlite3.Row

s = dict(conn.execute("SELECT * FROM session_summaries LIMIT 1").fetchone())
o_row = conn.execute(
    "SELECT * FROM observations WHERE memory_session_id = ? LIMIT 1",
    (s["memory_session_id"],),
).fetchone()
o = dict(o_row) if o_row else {}

print("=== Summary sample ===")
print(f"  id: {s.get('id')}")
print(f"  memory_session_id: {s.get('memory_session_id')}")
print(f"  project: {s.get('project')}")
print(f"  created_at: {s.get('created_at')}")

print("\n=== Linked observation ===")
if o:
    print(f"  id: {o.get('id')}")
    print(f"  memory_session_id: {o.get('memory_session_id')}")
    print(f"  type: {o.get('type')}")
    print(f"  title: {str(o.get('title', ''))[:80]}")

stats = conn.execute("""
    SELECT s.memory_session_id, COUNT(o.id) as obs_count
    FROM session_summaries s
    LEFT JOIN observations o ON s.memory_session_id = o.memory_session_id
    GROUP BY s.memory_session_id
""").fetchall()
counts = [r["obs_count"] for r in stats]
print("\n=== Session->Obs 关联统计 (hughesli) ===")
print(f"  总 session 数: {len(counts)}")
print(f"  有关联 obs 的 session: {sum(1 for c in counts if c > 0)}")
print(f"  平均每 session obs 数: {sum(counts)/len(counts):.1f}")
print(f"  最多: {max(counts)}, 最少: {min(counts)}")
conn.close()

conn2 = sqlite3.connect("../SourceMem/dpar-mem/dpar-ziyad.db")
conn2.row_factory = sqlite3.Row
stats2 = conn2.execute("""
    SELECT s.memory_session_id, COUNT(o.id) as obs_count
    FROM session_summaries s
    LEFT JOIN observations o ON s.memory_session_id = o.memory_session_id
    GROUP BY s.memory_session_id
""").fetchall()
counts2 = [r["obs_count"] for r in stats2]
print("\n=== Session->Obs 关联统计 (ziyad) ===")
print(f"  总 session 数: {len(counts2)}")
print(f"  有关联 obs 的 session: {sum(1 for c in counts2 if c > 0)}")
print(f"  平均每 session obs 数: {sum(counts2)/len(counts2):.1f}")
print(f"  最多: {max(counts2)}, 最少: {min(counts2)}")
conn2.close()
