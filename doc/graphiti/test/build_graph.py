#!/usr/bin/env python3
"""
从 dpar_export.db 提取 session_summary，通过 LLM 构建 Graphiti 风格知识图谱，
输出 graph_data.json 并生成可视化 HTML。
"""

import sqlite3
import json
import time
import sys
import os
import requests
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dpar_export.db"
OUTPUT_JSON = Path(__file__).parent / "graph_data.json"
OUTPUT_HTML = Path(__file__).parent / "dpar-knowledge-graph.html"

VENUS_URL = "http://v2.open.venus.oa.com/llmproxy/chat/completions"
VENUS_TOKEN = "XhfAJUXDU3lvrOp8AxRS0gQt@5172"
MODEL = "claude-sonnet-4-6"


def call_llm(system_prompt: str, user_prompt: str, retries: int = 3) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {VENUS_TOKEN}",
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
    }
    for attempt in range(retries):
        try:
            resp = requests.post(VENUS_URL, headers=headers, json=payload, timeout=120)
            if resp.status_code == 200:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            print(f"  [warn] HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"  [warn] attempt {attempt+1} failed: {e}", file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("LLM call failed after retries")


# ─── Step 1: Read summaries from DB ───

def load_summaries() -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, person_id, timestamp, content FROM memories "
        "WHERE content_type='session_summary' ORDER BY timestamp"
    ).fetchall()
    conn.close()

    seen_content = set()
    summaries = []
    for r in rows:
        content = r["content"].strip()
        if content in seen_content:
            continue
        seen_content.add(content)
        summaries.append({
            "id": r["id"] or f"summary-{r['person_id']}-{r['timestamp']}",
            "person_id": r["person_id"],
            "timestamp": r["timestamp"],
            "content": content,
        })
    return summaries


# ─── Step 2: Entity + Fact extraction via LLM ───

EXTRACT_SYSTEM = """你是一个知识图谱构建专家。你的任务是从给定的会话摘要中抽取实体和事实三元组，遵循 Graphiti 框架的规则。

抽取规则：
1. 实体类型包括：person（人物）、tech（技术/工具/框架）、document（文档/文件）、concept（概念/方案）、issue（问题/Bug）
2. 每条事实边(edge)必须包含 source_entity、target_entity、relation_type、fact（完整自然语言句子）
3. relation_type 用大写英文，如 DEVELOPS、FIXES、USES、DEPENDS_ON、CONFIGURES、DEBUGS、DISCOVERS、CREATES、MIGRATES 等
4. fact 必须是完整的中文自然语言句子，不是标签
5. 尽量抽取所有有价值的实体和关系，但避免过于琐碎的内容

输出严格 JSON 格式：
{
  "entities": [
    {"name": "实体名", "type": "person|tech|document|concept|issue", "summary": "一句话描述"}
  ],
  "edges": [
    {
      "source": "源实体名",
      "target": "目标实体名", 
      "relation": "RELATION_TYPE",
      "fact": "完整的中文自然语言事实描述"
    }
  ]
}

只输出 JSON，不要其他文字。"""


def extract_from_summary(summary: dict) -> dict:
    user_prompt = f"""请从以下会话摘要中抽取实体和事实三元组。

会话人：{summary['person_id']}
时间：{summary['timestamp']}
内容：
{summary['content']}"""

    print(f"  Extracting from {summary['person_id']} @ {summary['timestamp'][:10]}...")
    raw = call_llm(EXTRACT_SYSTEM, user_prompt)

    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]
    return json.loads(raw)


# ─── Step 3: Merge & deduplicate ───

def normalize_entity_name(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


RESOLVE_SYSTEM = """你是一个实体解析专家。给你一组从不同文本中抽取的实体名称列表，请识别出指代同一事物的实体并合并。

规则：
1. 名称不同但指代同一事物的实体应该合并（如 "DPAR打包流程" 和 "DPAR流水线" 是同一个概念）
2. 同一个工具/技术的不同表述应合并（如 "SVN" 和 "SVN SYSTEM账户认证" 中 SVN 是同一个工具）
3. 保留最具代表性/最简洁的名称作为合并后的名称
4. 不要过度合并——只有确实指代同一事物的才合并

输出严格 JSON 格式：
{
  "merge_groups": [
    {
      "canonical_name": "合并后的标准名称",
      "members": ["原名称1", "原名称2", "原名称3"]
    }
  ]
}

只输出 JSON，不要其他文字。members 中必须包含 canonical_name 自身。不需要合并的实体不要列出。"""


def resolve_entities(entity_names: list[str]) -> dict[str, str]:
    """Call LLM to identify duplicate entities, return mapping old_name -> canonical_name."""
    user_prompt = f"请识别以下实体中指代同一事物的分组并合并：\n\n{json.dumps(entity_names, ensure_ascii=False, indent=2)}"

    print("  Calling LLM for entity resolution...")
    raw = call_llm(RESOLVE_SYSTEM, user_prompt)

    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        raw = raw[start:end]
    result = json.loads(raw)

    name_map = {}
    for group in result.get("merge_groups", []):
        canon = group["canonical_name"]
        for member in group["members"]:
            name_map[normalize_entity_name(member)] = normalize_entity_name(canon)

    return name_map


def merge_results(all_extractions: list[dict], summaries: list[dict]) -> dict:
    entity_map = {}
    raw_edges = []

    for idx, (extraction, summary) in enumerate(zip(all_extractions, summaries)):
        episode_id = f"ep-{summary['person_id']}-{summary['timestamp'][:10]}"

        for ent in extraction.get("entities", []):
            key = normalize_entity_name(ent["name"])
            if key not in entity_map:
                entity_map[key] = {
                    "id": key,
                    "label": ent["name"],
                    "type": ent.get("type", "concept"),
                    "summary": ent.get("summary", ""),
                    "details": {},
                    "episodes": [episode_id],
                }
            else:
                if episode_id not in entity_map[key]["episodes"]:
                    entity_map[key]["episodes"].append(episode_id)
                if ent.get("summary") and len(ent["summary"]) > len(entity_map[key]["summary"]):
                    entity_map[key]["summary"] = ent["summary"]

        for e in extraction.get("edges", []):
            raw_edges.append({
                "source": normalize_entity_name(e["source"]),
                "target": normalize_entity_name(e["target"]),
                "relation": e.get("relation", "RELATES_TO"),
                "fact": e.get("fact", ""),
                "valid_at": summary["timestamp"],
                "episodes": [episode_id],
            })

    # Entity Resolution via LLM
    all_names = [entity_map[k]["label"] for k in entity_map]
    name_map = resolve_entities(all_names)

    merge_count = sum(1 for v in name_map.values() if v != name_map.get(v, v))
    print(f"  Entity resolution: {len(name_map)} names mapped, merging into fewer nodes")

    # Apply merges to entity_map
    merged_entity_map = {}
    key_remap = {}
    for old_key, ent in entity_map.items():
        new_key = name_map.get(old_key, old_key)
        key_remap[old_key] = new_key
        if new_key not in merged_entity_map:
            merged_entity_map[new_key] = {
                "id": new_key,
                "label": ent["label"],
                "type": ent["type"],
                "summary": ent["summary"],
                "details": {},
                "episodes": list(ent["episodes"]),
            }
        else:
            existing = merged_entity_map[new_key]
            for ep in ent["episodes"]:
                if ep not in existing["episodes"]:
                    existing["episodes"].append(ep)
            if ent.get("summary") and len(ent["summary"]) > len(existing["summary"]):
                existing["summary"] = ent["summary"]
            if ent["type"] != "concept" and existing["type"] == "concept":
                existing["type"] = ent["type"]

    # Apply merges to edges and ensure source/target nodes exist
    edges = []
    edge_id = 0
    for e in raw_edges:
        src_key = key_remap.get(e["source"], name_map.get(e["source"], e["source"]))
        tgt_key = key_remap.get(e["target"], name_map.get(e["target"], e["target"]))

        for k in [src_key, tgt_key]:
            if k not in merged_entity_map:
                merged_entity_map[k] = {
                    "id": k, "label": k, "type": "concept",
                    "summary": "", "details": {}, "episodes": e["episodes"][:],
                }

        edge_id += 1
        edges.append({
            "id": f"e{edge_id}",
            "source": src_key,
            "target": tgt_key,
            "relation": e["relation"],
            "fact": e["fact"],
            "valid_at": e["valid_at"],
            "invalid_at": None,
            "episodes": e["episodes"],
        })

    nodes = list(merged_entity_map.values())
    for n in nodes:
        n["details"]["出现次数"] = f"{len(n['episodes'])} 个 episode"
        n["details"]["来源"] = ", ".join(n["episodes"])
        del n["episodes"]

    print(f"  After resolution: {len(nodes)} nodes, {len(edges)} edges")
    return {"nodes": nodes, "edges": edges}


# ─── Step 4: Generate HTML ───

def generate_html(graph_data: dict):
    template = Path(__file__).parent / "graphiti-knowledge-graph-beautiful.html"
    html = template.read_text(encoding="utf-8")

    all_episodes = set()
    for e in graph_data["edges"]:
        for ep in e["episodes"]:
            all_episodes.add(ep)

    type_colors = {
        "person": "#64e4ff",
        "tech": "#3a7bff",
        "document": "#6e5cff",
        "concept": "#ff8c42",
        "issue": "#ff4a6b",
    }
    type_labels = {
        "person": "人物",
        "tech": "技术 / 工具",
        "document": "文档 / 文件",
        "concept": "概念 / 方案",
        "issue": "问题 / Bug",
    }

    legend_items = []
    used_types = set(n["type"] for n in graph_data["nodes"])
    for t in ["person", "tech", "document", "concept", "issue"]:
        if t in used_types:
            c = type_colors[t]
            legend_items.append(
                f'    <div class="item"><div class="dot" style="background:{c};color:{c}"></div>'
                f'<span class="item-label">{type_labels[t]}</span></div>'
            )

    legend_html = "\n".join(legend_items)

    nodes_json = json.dumps(graph_data["nodes"], ensure_ascii=False, indent=2)
    edges_json = json.dumps(graph_data["edges"], ensure_ascii=False, indent=2)

    new_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DPAR 知识图谱 — dpar_export.db 记忆数据</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Exo+2:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&family=Share+Tech+Mono&family=Noto+Sans+SC:wght@300;400;500;600&display=swap" rel="stylesheet">
"""

    style_start = html.find("<style>")
    style_end = html.find("</style>") + len("</style>")
    new_html += html[style_start:style_end]

    new_html += f"""
</head>
<body>

<div id="graph-container">
  <svg id="graph-svg">
    <defs>
      <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="6" result="coloredBlur"/>
        <feMerge><feMergeNode in="coloredBlur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="glow-strong" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="12" result="coloredBlur"/>
        <feMerge><feMergeNode in="coloredBlur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <marker id="arrow" viewBox="0 0 10 6" refX="28" refY="3"
              markerWidth="8" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,3 L0,6 Z" fill="#142240"/>
      </marker>
      <marker id="arrow-highlight" viewBox="0 0 10 6" refX="28" refY="3"
              markerWidth="8" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,3 L0,6 Z" fill="#00c8ff"/>
      </marker>
    </defs>
  </svg>

  <div id="legend">
    <div class="title">Node Types</div>
{legend_html}
  </div>

  <div id="stats-bar">
    <div class="stat-chip"><span class="stat-num" id="node-count">0</span>Nodes</div>
    <div class="stat-chip"><span class="stat-num" id="edge-count">0</span>Edges</div>
    <div class="stat-chip"><span class="stat-num" id="episode-count">0</span>Episodes</div>
  </div>

  <div id="title-bar">
    <div class="main-title"><div class="logo-dot"></div>DPAR Knowledge Graph</div>
    <div class="sub-title">dpar_export.db // session summaries // graphiti-style</div>
  </div>

  <div id="keyboard-hint">
    <div class="kbd-item"><span class="kbd">Scroll</span> Zoom</div>
    <div class="kbd-item"><span class="kbd">Drag</span> Move</div>
    <div class="kbd-item"><span class="kbd">Click</span> Detail</div>
  </div>

  <div id="tooltip"></div>
</div>

<div id="detail-panel" class="hidden">
  <button id="close-panel">&times;</button>
  <div id="panel-content"></div>
</div>

<script>
const nodes = {nodes_json};
const edges = {edges_json};

document.getElementById("node-count").textContent = nodes.length;
document.getElementById("edge-count").textContent = edges.length;
const allEpisodes = new Set();
edges.forEach(e => e.episodes.forEach(ep => allEpisodes.add(ep)));
document.getElementById("episode-count").textContent = allEpisodes.size;

const colorMap = {json.dumps(type_colors)};
const typeLabel = {json.dumps(type_labels, ensure_ascii=False)};

function nodeRadius(d) {{
  const linkCount = edges.filter(e => e.source === d.id || e.target === d.id
    || (e.source.id || e.source) === d.id || (e.target.id || e.target) === d.id).length;
  return Math.max(18, 12 + linkCount * 3);
}}

const svg = d3.select("#graph-svg");
const container = svg.append("g");
const zoom = d3.zoom().scaleExtent([0.2, 4]).on("zoom", (e) => container.attr("transform", e.transform));
svg.call(zoom);

const width = window.innerWidth - 400;
const height = window.innerHeight;

const simulation = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(edges).id(d => d.id).distance(140))
  .force("charge", d3.forceManyBody().strength(-400))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .force("collision", d3.forceCollide().radius(d => nodeRadius(d) + 16));

function computeCurvatures() {{
  const pairCount = {{}}, pairIdx = {{}};
  edges.forEach(e => {{
    const key = [e.source.id || e.source, e.target.id || e.target].sort().join("||");
    pairCount[key] = (pairCount[key] || 0) + 1;
    pairIdx[key] = 0;
  }});
  edges.forEach(e => {{
    const key = [e.source.id || e.source, e.target.id || e.target].sort().join("||");
    const total = pairCount[key];
    const idx = pairIdx[key]++;
    e._curve = total === 1 ? 0 : (idx - (total - 1) / 2) * 45;
  }});
}}
computeCurvatures();

const linkGroup = container.append("g");
const linkPaths = linkGroup.selectAll("path").data(edges).join("path")
  .attr("fill", "none")
  .attr("stroke", d => d.invalid_at ? "#0c1628" : "#142240")
  .attr("stroke-width", d => d.invalid_at ? 1 : 1.5)
  .attr("stroke-dasharray", d => d.invalid_at ? "6 4" : "none")
  .attr("marker-end", "url(#arrow)")
  .attr("cursor", "pointer")
  .on("click", (event, d) => {{ event.stopPropagation(); showEdgeDetail(d); }})
  .on("mouseenter", (event, d) => highlightEdge(d, true))
  .on("mouseleave", (event, d) => highlightEdge(d, false));

const linkLabels = linkGroup.selectAll("text").data(edges).join("text")
  .text(d => d.relation)
  .attr("font-size", 8)
  .attr("font-family", "'Share Tech Mono', monospace")
  .attr("fill", d => d.invalid_at ? "#0e1830" : "#2a4068")
  .attr("text-anchor", "middle").attr("dy", -8)
  .attr("cursor", "pointer")
  .attr("letter-spacing", "1px")
  .on("click", (event, d) => {{ event.stopPropagation(); showEdgeDetail(d); }});

const nodeGroup = container.append("g");
const nodeEls = nodeGroup.selectAll("g").data(nodes).join("g")
  .attr("cursor", "grab")
  .call(d3.drag()
    .on("start", (event, d) => {{ if (!event.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }})
    .on("drag", (event, d) => {{ d.fx = event.x; d.fy = event.y; }})
    .on("end", (event, d) => {{ if (!event.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }}));

nodeEls.append("circle").attr("r", d => nodeRadius(d) + 8)
  .attr("fill", "none").attr("stroke", d => colorMap[d.type] || "#888")
  .attr("stroke-width", 0.5).attr("stroke-opacity", 0.12).attr("stroke-dasharray", "4 3");

nodeEls.append("circle").attr("class", "node-main")
  .attr("r", d => nodeRadius(d))
  .attr("fill", d => colorMap[d.type] || "#888").attr("fill-opacity", 0.06)
  .attr("stroke", d => colorMap[d.type] || "#888").attr("stroke-width", 1.5).attr("stroke-opacity", 0.7)
  .attr("filter", "url(#glow)");

nodeEls.append("circle").attr("r", d => nodeRadius(d) * 0.22)
  .attr("fill", d => colorMap[d.type] || "#888").attr("fill-opacity", 0.6).attr("filter", "url(#glow)");

nodeEls.append("text").text(d => d.label)
  .attr("text-anchor", "middle").attr("dy", d => nodeRadius(d) + 20)
  .attr("fill", "#4a6890").attr("font-size", 10)
  .attr("font-weight", 500).attr("font-family", "'Exo 2', 'Noto Sans SC', sans-serif")
  .attr("letter-spacing", "0.5px");

nodeEls
  .on("click", (event, d) => {{ event.stopPropagation(); showNodeDetail(d); }})
  .on("mouseenter", (event, d) => highlightNode(d, true))
  .on("mouseleave", (event, d) => highlightNode(d, false));

simulation.on("tick", () => {{
  linkPaths.attr("d", d => {{
    const dx = d.target.x - d.source.x, dy = d.target.y - d.source.y;
    if (d._curve === 0) return `M${{d.source.x}},${{d.source.y}} L${{d.target.x}},${{d.target.y}}`;
    const mx = (d.source.x + d.target.x) / 2, my = (d.source.y + d.target.y) / 2;
    const len = Math.sqrt(dx*dx + dy*dy) || 1;
    return `M${{d.source.x}},${{d.source.y}} Q${{mx + (-dy/len*d._curve)}},${{my + (dx/len*d._curve)}} ${{d.target.x}},${{d.target.y}}`;
  }});
  linkLabels
    .attr("x", d => {{
      const mx = (d.source.x + d.target.x) / 2;
      if (d._curve === 0) return mx;
      const dx = d.target.x - d.source.x, dy = d.target.y - d.source.y;
      const len = Math.sqrt(dx*dx + dy*dy) || 1;
      return mx + (-dy/len*d._curve)*0.5;
    }})
    .attr("y", d => {{
      const my = (d.source.y + d.target.y) / 2;
      if (d._curve === 0) return my;
      const dx = d.target.x - d.source.x, dy = d.target.y - d.source.y;
      const len = Math.sqrt(dx*dx + dy*dy) || 1;
      return my + (dx/len*d._curve)*0.5;
    }});
  nodeEls.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
}});

function highlightNode(d, on) {{
  const connEdges = new Set(), connNodes = new Set([d.id]);
  edges.forEach(e => {{
    const s = e.source.id||e.source, t = e.target.id||e.target;
    if (s === d.id || t === d.id) {{ connEdges.add(e.id); connNodes.add(s); connNodes.add(t); }}
  }});
  if (on) {{
    nodeEls.select(".node-main").transition().duration(200)
      .attr("fill-opacity", n => connNodes.has(n.id) ? 0.15 : 0.015)
      .attr("stroke-opacity", n => connNodes.has(n.id) ? 1 : 0.06)
      .attr("filter", n => connNodes.has(n.id) ? "url(#glow-strong)" : "none");
    nodeEls.select("text").transition().duration(200)
      .attr("fill-opacity", n => connNodes.has(n.id) ? 1 : 0.1)
      .attr("fill", n => n.id === d.id ? "#fff" : (connNodes.has(n.id) ? "#6890b8" : "#4a6890"));
    linkPaths.transition().duration(200)
      .attr("stroke", e => connEdges.has(e.id) ? "#00c8ff" : "#060e1a")
      .attr("stroke-opacity", e => connEdges.has(e.id) ? 0.9 : 0.06)
      .attr("stroke-width", e => connEdges.has(e.id) ? 2.5 : 1)
      .attr("marker-end", e => connEdges.has(e.id) ? "url(#arrow-highlight)" : "url(#arrow)");
    linkLabels.transition().duration(200).attr("fill-opacity", e => connEdges.has(e.id) ? 1 : 0.03);
  }} else {{
    nodeEls.select(".node-main").transition().duration(300)
      .attr("fill-opacity", 0.06).attr("stroke-opacity", 0.7).attr("filter", "url(#glow)");
    nodeEls.select("text").transition().duration(300).attr("fill-opacity", 1).attr("fill", "#4a6890");
    linkPaths.transition().duration(300)
      .attr("stroke", e => e.invalid_at ? "#0c1628" : "#142240")
      .attr("stroke-opacity", 1).attr("stroke-width", e => e.invalid_at ? 1 : 1.5)
      .attr("marker-end", "url(#arrow)");
    linkLabels.transition().duration(300).attr("fill-opacity", 1);
  }}
}}

function highlightEdge(e, on) {{
  if (on) {{
    linkPaths.transition().duration(150)
      .attr("stroke", le => le.id === e.id ? "#00c8ff" : "#060e1a")
      .attr("stroke-opacity", le => le.id === e.id ? 1 : 0.1)
      .attr("stroke-width", le => le.id === e.id ? 3 : 1);
    linkLabels.transition().duration(150)
      .attr("fill-opacity", le => le.id === e.id ? 1 : 0.06)
      .attr("fill", le => le.id === e.id ? "#64e4ff" : "#2a4068");
  }} else {{
    linkPaths.transition().duration(250)
      .attr("stroke", le => le.invalid_at ? "#0c1628" : "#142240")
      .attr("stroke-opacity", 1).attr("stroke-width", le => le.invalid_at ? 1 : 1.5);
    linkLabels.transition().duration(250).attr("fill-opacity", 1)
      .attr("fill", le => le.invalid_at ? "#0e1830" : "#2a4068");
  }}
}}

const panel = d3.select("#detail-panel");
const panelContent = d3.select("#panel-content");
d3.select("#close-panel").on("click", () => panel.classed("hidden", true));
svg.on("click", () => panel.classed("hidden", true));

function showNodeDetail(d) {{
  const c = colorMap[d.type] || "#888";
  const relatedEdges = edges.filter(e =>
    (e.source.id||e.source) === d.id || (e.target.id||e.target) === d.id);

  let html = `<h2>${{d.label}}</h2>`;
  html += `<span class="type-badge" style="background:${{c}}18;color:${{c}};border:1px solid ${{c}}35">${{typeLabel[d.type] || d.type}}</span>`;
  html += `<div class="section-title">摘要 Summary</div>`;
  html += `<div class="fact-text" style="border-left-color:${{c}}">${{d.summary || '暂无摘要'}}</div>`;

  if (d.details) {{
    html += `<div class="section-title">属性 Properties</div>`;
    Object.entries(d.details).forEach(([k, v]) => {{
      html += `<div class="meta-row"><span class="label">${{k}}</span><span class="value">${{v}}</span></div>`;
    }});
  }}

  html += `<div class="section-title">关联事实边 (${{relatedEdges.length}})</div>`;
  relatedEdges.forEach(e => {{
    const sid = e.source.id||e.source, tid = e.target.id||e.target;
    const direction = sid === d.id ? `→ ${{tid}}` : `← ${{sid}}`;
    html += `<div class="edge-list-item" data-edge-id="${{e.id}}">
      <span class="rel">${{e.relation}}</span><span class="target">${{direction}}</span>
      <div style="margin-top:6px;color:#3a5078;font-size:12px;line-height:1.6">${{e.fact}}</div>
    </div>`;
  }});

  panelContent.html(html);
  panel.classed("hidden", false);
  panelContent.selectAll(".edge-list-item").on("click", function() {{
    const edge = edges.find(e => e.id === this.getAttribute("data-edge-id"));
    if (edge) showEdgeDetail(edge);
  }});
}}

function showEdgeDetail(e) {{
  const sid = e.source.id||e.source, tid = e.target.id||e.target;
  let html = `<h2 style="font-size:16px;color:#64e4ff">${{sid}} → ${{tid}}</h2>`;
  html += `<span class="type-badge" style="background:#00c8ff18;color:#64e4ff;border:1px solid #00c8ff35">${{e.relation}}</span>`;
  if (e.invalid_at) html += `<span class="type-badge" style="background:#ff4a4a12;color:#ff6b6b;border:1px solid #ff4a4a30;margin-left:6px">已失效</span>`;

  html += `<div class="section-title">事实 Fact</div>`;
  html += `<div class="fact-text">${{e.fact}}</div>`;

  html += `<div class="section-title">时间信息 Temporal</div>`;
  html += `<div class="meta-row"><span class="label">valid_at</span><span class="value">${{(e.valid_at||'—').replace('T',' ').replace('Z',' UTC')}}</span></div>`;
  html += `<div class="meta-row"><span class="label">invalid_at</span><span class="value">${{e.invalid_at ? e.invalid_at.replace('T',' ').replace('Z',' UTC') : '<span style="color:#00c8ff;text-shadow:0 0 6px rgba(0,200,255,0.3)">null (仍有效)</span>'}}</span></div>`;

  html += `<div class="section-title">来源 Episodes</div>`;
  e.episodes.forEach(ep => {{
    html += `<div class="meta-row"><span class="label">episode</span><span class="value" style="font-family:'Share Tech Mono',monospace;font-size:11px">${{ep}}</span></div>`;
  }});

  html += `<div class="section-title">关联节点</div>`;
  html += `<div class="edge-list-item" data-node-id="${{sid}}"><span class="rel">Source</span><span class="target">${{sid}}</span></div>`;
  html += `<div class="edge-list-item" data-node-id="${{tid}}"><span class="rel">Target</span><span class="target">${{tid}}</span></div>`;

  panelContent.html(html);
  panel.classed("hidden", false);
  panelContent.selectAll(".edge-list-item[data-node-id]").on("click", function() {{
    const node = nodes.find(n => n.id === this.getAttribute("data-node-id"));
    if (node) showNodeDetail(node);
  }});
}}

window.addEventListener("resize", () => {{
  simulation.force("center", d3.forceCenter(window.innerWidth / 2, window.innerHeight / 2));
  simulation.alpha(0.3).restart();
}});
</script>
</body>
</html>"""

    OUTPUT_HTML.write_text(new_html, encoding="utf-8")
    print(f"\n✓ HTML written to {OUTPUT_HTML}")


# ─── Main ───

def main():
    print("=" * 60)
    print("DPAR Knowledge Graph Builder (Graphiti-style)")
    print("=" * 60)

    print("\n[1/4] Loading session summaries from DB...")
    summaries = load_summaries()
    print(f"  Found {len(summaries)} unique summaries")
    for s in summaries:
        print(f"  - {s['person_id']} @ {s['timestamp'][:10]}: {s['content'][:60]}...")

    print(f"\n[2/4] Extracting entities & facts via LLM ({MODEL})...")
    all_extractions = []
    for i, s in enumerate(summaries):
        print(f"\n  [{i+1}/{len(summaries)}]", end="")
        try:
            result = extract_from_summary(s)
            ent_count = len(result.get("entities", []))
            edge_count = len(result.get("edges", []))
            print(f"  → {ent_count} entities, {edge_count} edges")
            all_extractions.append(result)
        except Exception as e:
            print(f"  → ERROR: {e}", file=sys.stderr)
            all_extractions.append({"entities": [], "edges": []})
        time.sleep(1)

    print(f"\n[3/4] Merging & deduplicating...")
    graph_data = merge_results(all_extractions, summaries)
    print(f"  → {len(graph_data['nodes'])} unique nodes, {len(graph_data['edges'])} edges")

    OUTPUT_JSON.write_text(json.dumps(graph_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  → JSON saved to {OUTPUT_JSON}")

    print(f"\n[4/4] Generating HTML visualization...")
    generate_html(graph_data)

    print(f"\n{'=' * 60}")
    print(f"Done! Open {OUTPUT_HTML} in a browser.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
