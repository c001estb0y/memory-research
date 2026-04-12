"""统计完整蒸馏过程的时间和 token 消耗"""
import re
from pathlib import Path

terminals = Path(r"C:\Users\minusjiang\.cursor\projects\d-GitHub-memory-research\terminals")

PATTERN = re.compile(r'\[Venus\].*?(\d+)ms.*?input=(\d+).*?output=(\d+)')

phases = {
    "hughesli_L1": {"file": "373314.txt", "lines": (20, 290), "desc": "hughesli L1 蒸馏 (75 batches)"},
    "hughesli_L2_first": {"file": "373314.txt", "lines": (290, 340), "desc": "hughesli L2 首次（大部分超时）"},
    "ziyad_L1_part1": {"file": "373314.txt", "lines": (340, 540), "desc": "ziyad L1 蒸馏 Part1 (42 batches)"},
    "ziyad_L1_part2": {"file": "498575.txt", "lines": (1, 170), "desc": "ziyad L1 续跑 (33 batches)"},
    "ziyad_L2_first": {"file": "498575.txt", "lines": (170, 220), "desc": "ziyad L2 首次（大部分超时）"},
    "L2_rerun_all": {"file": "492211.txt", "lines": (1, 100), "desc": "L2 补跑 (hughesli 6 + ziyad 5 themes)"},
    "L2_rerun_debug": {"file": "430469.txt", "lines": (1, 20), "desc": "ziyad debugging 重试"},
}

grand_total_input = 0
grand_total_output = 0
grand_total_calls = 0
grand_total_ms = 0

print(f"{'='*80}")
print(f"  DPAR 蒸馏完整成本分析")
print(f"{'='*80}")

for phase_name, cfg in phases.items():
    fpath = terminals / cfg["file"]
    content = fpath.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    
    start, end = cfg["lines"]
    subset = lines[start-1:end]
    
    calls = 0
    total_input = 0
    total_output = 0
    total_ms = 0
    
    for line in subset:
        m = PATTERN.search(line)
        if m:
            ms = int(m.group(1))
            inp = int(m.group(2))
            out = int(m.group(3))
            calls += 1
            total_input += inp
            total_output += out
            total_ms += ms
    
    total_tokens = total_input + total_output
    grand_total_input += total_input
    grand_total_output += total_output
    grand_total_calls += calls
    grand_total_ms += total_ms
    
    print(f"\n--- {cfg['desc']} ---")
    print(f"  API 调用: {calls} 次")
    print(f"  总 input tokens: {total_input:,}")
    print(f"  总 output tokens: {total_output:,}")
    print(f"  总 tokens: {total_tokens:,}")
    print(f"  总耗时: {total_ms/1000:.0f}s ({total_ms/60000:.1f} min)")
    if calls > 0:
        print(f"  平均每次: input={total_input//calls:,} out={total_output//calls:,} total={total_tokens//calls:,} tokens, {total_ms//calls/1000:.0f}s")

grand_total = grand_total_input + grand_total_output
print(f"\n{'='*80}")
print(f"  总计")
print(f"{'='*80}")
print(f"  API 调用总数: {grand_total_calls} 次")
print(f"  总 input tokens: {grand_total_input:,}")
print(f"  总 output tokens: {grand_total_output:,}")
print(f"  总 tokens: {grand_total:,}")
print(f"  总 API 耗时: {grand_total_ms/1000:.0f}s ({grand_total_ms/60000:.1f} min)")
print(f"  input/output 比: {grand_total_input/grand_total_output:.1f}:1")

print(f"\n--- 产出 ---")
print(f"  L1 经验: 636 条 (hughesli 327 + ziyad 309)")
print(f"  L2 叙事: 49 条 (hughesli 24 + ziyad 25)")
print(f"  原始输入: 12,344 条 (hughesli 2,223 + ziyad 10,121)")

print(f"\n--- 效率指标 ---")
print(f"  每条 L1 经验: {grand_total//636:,} tokens")
print(f"  每条 L2 叙事: {grand_total//49:,} tokens (仅 L2 调用)")
print(f"  原始记录 → L1 经验压缩比: {12344/636:.1f}:1")
print(f"  每条原始记录消耗: {grand_total/12344:.0f} tokens")

# 按层级细分
l1_input = 0
l1_output = 0
l1_calls = 0
l2_input = 0
l2_output = 0
l2_calls = 0

for phase_name, cfg in phases.items():
    fpath = terminals / cfg["file"]
    content = fpath.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    start, end = cfg["lines"]
    subset = lines[start-1:end]
    
    for line in subset:
        m = PATTERN.search(line)
        if m:
            inp = int(m.group(2))
            out = int(m.group(3))
            if "L1" in phase_name:
                l1_input += inp
                l1_output += out
                l1_calls += 1
            else:
                l2_input += inp
                l2_output += out
                l2_calls += 1

print(f"\n--- 按层级细分 ---")
print(f"  L1: {l1_calls} calls, input={l1_input:,} output={l1_output:,} total={l1_input+l1_output:,}")
print(f"  L2: {l2_calls} calls, input={l2_input:,} output={l2_output:,} total={l2_input+l2_output:,}")
print(f"  L2 占总 token 比: {100*(l2_input+l2_output)/grand_total:.1f}%")
