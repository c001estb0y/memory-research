"""深入分析 observation 的信息质量"""
import sqlite3
import json
from collections import Counter

def sample_obs_by_type(db_path, person, n=5):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    print(f"\n{'='*70}")
    print(f"  {person} Observation 深度采样")
    print(f"{'='*70}")
    
    obs = [dict(r) for r in conn.execute("SELECT * FROM observations").fetchall()]
    
    # 按 type 分组，每组取有代表性的样本
    by_type = {}
    for o in obs:
        t = o.get('type', 'unknown')
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(o)
    
    for otype, items in sorted(by_type.items(), key=lambda x: -len(x[1])):
        print(f"\n--- {otype} ({len(items)} 条) ---")
        
        # 分析这种类型的信息价值
        has_actionable = 0
        has_problem = 0
        is_noise = 0
        
        problem_kw = ['错误', '失败', '报错', 'error', 'fail', 'crash', '崩溃', '问题', 
                       'not found', '找不到', 'missing', 'bug', 'timeout']
        noise_kw = ['agent acknowledgment', 'successful retrieval', 'read file', 
                     'searched for', 'executed shell command', 'user requested',
                     'listed directory', 'read the content']
        
        for item in items:
            text = (str(item.get('title', '')) + str(item.get('text', ''))).lower()
            if any(kw in text for kw in problem_kw):
                has_problem += 1
            if any(kw in text for kw in noise_kw):
                is_noise += 1
            # 判断是否有可操作信息（包含具体路径、命令、配置值）
            if any(c in str(item.get('text', '')) for c in ['/', '\\', '.py', '.ini', '.csv', 'config']):
                has_actionable += 1
        
        print(f"  含问题关键词: {has_problem}/{len(items)} ({100*has_problem/len(items):.0f}%)")
        print(f"  含可操作信息: {has_actionable}/{len(items)} ({100*has_actionable/len(items):.0f}%)")
        print(f"  噪音标记: {is_noise}/{len(items)} ({100*is_noise/len(items):.0f}%)")
        
        # 打印 2 个样本
        import random
        random.seed(42)
        for s in random.sample(items, min(2, len(items))):
            print(f"\n  样本 title: {str(s.get('title', ''))[:120]}")
            print(f"  样本 text: {str(s.get('text', ''))[:250]}")
    
    conn.close()


def compare_summary_vs_obs_for_nevercook():
    """对 nevercook 问题：summary vs observation 各自能给出什么"""
    print(f"\n{'='*70}")
    print(f"  NeverCook 问题：Summary vs Observation 信息对比")
    print(f"{'='*70}")
    
    for person, db_path in [("hughesli", "../SourceMem/dpar-mem/dpar-hughesli.db"), 
                             ("ziyad", "../SourceMem/dpar-mem/dpar-ziyad.db")]:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        summaries = [dict(r) for r in conn.execute("SELECT * FROM session_summaries").fetchall()]
        observations = [dict(r) for r in conn.execute("SELECT * FROM observations").fetchall()]
        
        # 找含 nevercook 的 summary
        nc_summaries = []
        for s in summaries:
            text = (str(s.get('request', '')) + str(s.get('learned', '')) + str(s.get('completed', ''))).lower()
            if 'nevercook' in text or 'never_cook' in text or 'neverc' in text:
                nc_summaries.append(s)
        
        nc_obs = []
        for o in observations:
            text = (str(o.get('title', '')) + str(o.get('text', '')) + str(o.get('facts', ''))).lower()
            if 'nevercook' in text or 'never_cook' in text or 'neverc' in text:
                nc_obs.append(o)
        
        print(f"\n  {person}:")
        print(f"    含 NeverCook 的 summary: {len(nc_summaries)} 条")
        print(f"    含 NeverCook 的 observation: {len(nc_obs)} 条")
        
        if nc_summaries:
            print(f"\n    --- Summary 样本 (前2条) ---")
            for s in nc_summaries[:2]:
                print(f"    request: {str(s.get('request', ''))[:200]}")
                print(f"    learned: {str(s.get('learned', ''))[:200]}")
                print()
        
        if nc_obs:
            print(f"\n    --- Observation 样本 (前3条) ---")
            for o in nc_obs[:3]:
                print(f"    [{o.get('type', '')}] {str(o.get('title', ''))[:120]}")
                print(f"    text: {str(o.get('text', ''))[:200]}")
                print()
        
        conn.close()


if __name__ == "__main__":
    sample_obs_by_type("../SourceMem/dpar-mem/dpar-hughesli.db", "hughesli")
    compare_summary_vs_obs_for_nevercook()
