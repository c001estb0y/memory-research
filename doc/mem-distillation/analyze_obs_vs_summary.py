"""分析 observation vs summary 的信息密度和实际贡献"""
import sqlite3
import json
from pathlib import Path
from collections import Counter

def analyze_db(db_path, person):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    print(f"\n{'='*70}")
    print(f"  {person} 数据库分析")
    print(f"{'='*70}")
    
    # --- summaries ---
    summaries = [dict(r) for r in conn.execute("SELECT * FROM session_summaries").fetchall()]
    print(f"\n[Summaries] 总数: {len(summaries)}")
    
    s_lengths = [len(str(s.get('request', ''))) + len(str(s.get('completed', ''))) + len(str(s.get('learned', ''))) for s in summaries]
    print(f"  平均长度: {sum(s_lengths)/len(s_lengths):.0f} 字符")
    
    s_with_learned = sum(1 for s in summaries if s.get('learned') and len(str(s['learned'])) > 20)
    s_with_completed = sum(1 for s in summaries if s.get('completed') and len(str(s['completed'])) > 20)
    print(f"  有 learned 字段: {s_with_learned}/{len(summaries)} ({100*s_with_learned/len(summaries):.0f}%)")
    print(f"  有 completed 字段: {s_with_completed}/{len(summaries)} ({100*s_with_completed/len(summaries):.0f}%)")
    
    # sample
    print(f"\n  === 随机 summary 样本 ===")
    import random
    random.seed(42)
    for s in random.sample(summaries, min(3, len(summaries))):
        print(f"  ---")
        print(f"  request: {str(s.get('request', ''))[:150]}")
        print(f"  learned: {str(s.get('learned', ''))[:150]}")
        print(f"  completed: {str(s.get('completed', ''))[:150]}")
    
    # --- observations ---
    observations = [dict(r) for r in conn.execute("SELECT * FROM observations").fetchall()]
    print(f"\n[Observations] 总数: {len(observations)}")
    
    o_types = Counter(o.get('type', 'unknown') for o in observations)
    print(f"  类型分布: {dict(o_types)}")
    
    o_with_text = sum(1 for o in observations if o.get('text') and len(str(o['text'])) > 20)
    o_with_facts = sum(1 for o in observations if o.get('facts') and len(str(o['facts'])) > 10)
    o_lengths = [len(str(o.get('text', ''))) + len(str(o.get('title', ''))) for o in observations]
    print(f"  有 text 字段: {o_with_text}/{len(observations)} ({100*o_with_text/len(observations):.0f}%)")
    print(f"  有 facts 字段: {o_with_facts}/{len(observations)} ({100*o_with_facts/len(observations):.0f}%)")
    print(f"  平均长度: {sum(o_lengths)/len(o_lengths):.0f} 字符")
    
    # sample different types
    print(f"\n  === 随机 observation 样本 ===")
    for otype in list(o_types.keys())[:3]:
        typed = [o for o in observations if o.get('type') == otype]
        sample = random.choice(typed) if typed else None
        if sample:
            print(f"  --- type={otype} ---")
            print(f"  title: {str(sample.get('title', ''))[:150]}")
            print(f"  text: {str(sample.get('text', ''))[:200]}")
            if sample.get('facts'):
                print(f"  facts: {str(sample.get('facts', ''))[:150]}")

    # --- 关键问题：observation 中有多少包含"问题"信息？ ---
    problem_keywords = ['错误', '失败', '报错', 'error', 'fail', 'bug', '问题', '异常',
                        'crash', '崩溃', 'timeout', '超时', 'permission', '权限',
                        'not found', '找不到', '缺失', 'missing', 'invalid']
    
    s_problem = 0
    for s in summaries:
        text = str(s.get('request', '')) + str(s.get('learned', ''))
        if any(kw in text.lower() for kw in problem_keywords):
            s_problem += 1
    
    o_problem = 0
    for o in observations:
        text = str(o.get('title', '')) + str(o.get('text', ''))
        if any(kw in text.lower() for kw in problem_keywords):
            o_problem += 1
    
    print(f"\n[问题相关记录]")
    print(f"  summary 中含问题关键词: {s_problem}/{len(summaries)} ({100*s_problem/len(summaries):.0f}%)")
    print(f"  observation 中含问题关键词: {o_problem}/{len(observations)} ({100*o_problem/len(observations):.0f}%)")
    
    # --- 信息独占性：observation 中有多少信息在 summary 中找不到？ ---
    all_summary_text = " ".join(str(s.get('request', '')) + str(s.get('learned', '')) + str(s.get('completed', '')) for s in summaries).lower()
    
    unique_obs_keywords = 0
    obs_unique_examples = []
    for o in observations:
        title = str(o.get('title', '')).lower()
        words = [w for w in title.split() if len(w) > 3]
        title_in_summary = sum(1 for w in words if w in all_summary_text)
        if words and title_in_summary / len(words) < 0.3:
            unique_obs_keywords += 1
            if len(obs_unique_examples) < 3:
                obs_unique_examples.append(o.get('title', ''))
    
    print(f"\n[信息独占性]")
    print(f"  observation 标题中 70%+ 词在 summary 中找不到: {unique_obs_keywords}/{len(observations)} ({100*unique_obs_keywords/len(observations):.0f}%)")
    if obs_unique_examples:
        print(f"  示例:")
        for ex in obs_unique_examples:
            print(f"    - {ex[:120]}")
    
    conn.close()


def analyze_l2_obs_contribution():
    """分析 L2 叙事中 observation 的实际贡献"""
    print(f"\n{'='*70}")
    print(f"  L2 叙事中 observation 的贡献分析")
    print(f"{'='*70}")
    
    for person in ["hughesli", "ziyad"]:
        l1_path = f"output/dpar-{person}-experiences.json"
        exps = json.loads(Path(l1_path).read_text("utf-8"))
        
        from narrative import group_experiences_by_theme
        import csv
        obs_csv = f"dpar-export/{person}_observations.csv"
        observations = []
        if Path(obs_csv).exists():
            with open(obs_csv, "r", encoding="utf-8") as f:
                observations = list(csv.DictReader(f))
        
        theme_groups = group_experiences_by_theme(exps, observations)
        
        print(f"\n  {person}:")
        total_obs_matched = 0
        for theme, group in theme_groups.items():
            n_exp = len(group["experiences"])
            n_obs = len(group.get("raw_observations", []))
            total_obs_matched += n_obs
            print(f"    {theme}: {n_exp} exps, {n_obs} obs matched")
        
        print(f"    总计: {sum(len(g['experiences']) for g in theme_groups.values())} exps, {total_obs_matched} obs matched (out of {len(observations)} total obs)")
        print(f"    观测利用率: {100*total_obs_matched/len(observations):.1f}%" if observations else "    无观测数据")


def analyze_nevercook_example():
    """以 nevercook 为例分析问题索引路径"""
    print(f"\n{'='*70}")
    print(f"  NeverCook 案例：问题索引路径分析")
    print(f"{'='*70}")
    
    for person in ["hughesli", "ziyad"]:
        l1_path = f"output/dpar-{person}-experiences.json"
        exps = json.loads(Path(l1_path).read_text("utf-8"))
        
        cook_exps = []
        for exp in exps:
            text = json.dumps(exp, ensure_ascii=False).lower()
            if "nevercook" in text or "never_cook" in text or "directoriesTonevercook" in text.replace(" ", ""):
                cook_exps.append(exp)
        
        print(f"\n  {person}: 含 NeverCook 的经验 = {len(cook_exps)} 条")
        for i, exp in enumerate(cook_exps):
            print(f"    [{i+1}] issue: {exp.get('issue_context', '')[:120]}")
            print(f"        root_cause: {exp.get('root_cause', '')[:120]}")
            print(f"        components: {exp.get('related_components', [])}")
            print(f"        confidence: {exp.get('confidence', '?')}")


if __name__ == "__main__":
    analyze_db("../SourceMem/dpar-mem/dpar-hughesli.db", "hughesli")
    analyze_db("../SourceMem/dpar-mem/dpar-ziyad.db", "ziyad")
    analyze_l2_obs_contribution()
    analyze_nevercook_example()
