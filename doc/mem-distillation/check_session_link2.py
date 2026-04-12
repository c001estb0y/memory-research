"""修正：检查 memory_session_id 的分布"""
import sqlite3

for person, db_path in [
    ("hughesli", "../SourceMem/dpar-mem/dpar-hughesli.db"),
    ("ziyad", "../SourceMem/dpar-mem/dpar-ziyad.db"),
]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    s_sessions = conn.execute(
        "SELECT memory_session_id, COUNT(*) as cnt FROM session_summaries GROUP BY memory_session_id"
    ).fetchall()
    o_sessions = conn.execute(
        "SELECT memory_session_id, COUNT(*) as cnt FROM observations GROUP BY memory_session_id"
    ).fetchall()

    s_map = {r["memory_session_id"]: r["cnt"] for r in s_sessions}
    o_map = {r["memory_session_id"]: r["cnt"] for r in o_sessions}

    all_sessions = set(s_map.keys()) | set(o_map.keys())
    shared = set(s_map.keys()) & set(o_map.keys())

    print(f"\n=== {person} ===")
    print(f"  distinct session IDs in summaries: {len(s_map)}")
    print(f"  distinct session IDs in observations: {len(o_map)}")
    print(f"  共享的 session IDs: {len(shared)}")
    print(f"  summaries 总数: {sum(s_map.values())}")
    print(f"  observations 总数: {sum(o_map.values())}")

    if shared:
        for sid in list(shared)[:3]:
            print(f"\n  session {sid[:30]}...:")
            print(f"    summaries: {s_map[sid]}, observations: {o_map.get(sid, 0)}")

    conn.close()
