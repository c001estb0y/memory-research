#!/usr/bin/env python3
"""
使用 Graphiti 原生管线处理 minusjiang_shadowfolk_export.db 中的 session_summary，
通过 Kuzu 嵌入式图数据库存储，最后导出数据并生成可视化 HTML。
"""

import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sentence_transformers import SentenceTransformer

import openai
from pydantic import BaseModel as PydanticBaseModel

from graphiti_core import Graphiti
from graphiti_core.driver.kuzu_driver import KuzuDriver
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client import LLMConfig, OpenAIClient
from graphiti_core.nodes import EpisodeType

DB_PATH = Path(__file__).parent.parent.parent.parent / "minusjiang_shadowfolk_export.db"
KUZU_DB_PATH = str(Path(__file__).parent / "kuzu_db")
OUTPUT_JSON = Path(__file__).parent / "shadowfolk_graph_data.json"
OUTPUT_HTML = Path(__file__).parent / "shadowfolk-knowledge-graph.html"

TIMI_URL = "http://api.timiai.woa.com/ai_api_manage/llmproxy/chat/completions"
TIMI_TOKEN = "OIiBa1Er1Zqrriiyaj7QftmMq09x5dDZ2S8GA5KW"
MODEL = "gpt-5-nano"
SMALL_MODEL = "gpt-4o-mini"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"


class LocalEmbedder(EmbedderClient):
    """Uses a local sentence-transformers model for embeddings."""

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        print(f"  Loading local embedding model: {model_name}...")
        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()
        print(f"  Embedding model ready (dim={self.dim})")

    async def create(self, input_data):
        if isinstance(input_data, str):
            embedding = self.model.encode(input_data, normalize_embeddings=True)
            return embedding.tolist()
        elif isinstance(input_data, list) and all(isinstance(x, str) for x in input_data):
            embedding = self.model.encode(input_data[0], normalize_embeddings=True)
            return embedding.tolist()
        return self.model.encode("", normalize_embeddings=True).tolist()

    async def create_batch(self, input_data_list):
        embeddings = self.model.encode(input_data_list, normalize_embeddings=True)
        return embeddings.tolist()


class TimiOpenAIClient(OpenAIClient):
    """Uses chat/completions with JSON schema in prompt for Timi API proxy compatibility.
    
    TimiAPI defaults to streaming when stream param is absent, so we must
    always pass stream=False explicitly.
    """

    async def _create_structured_completion(
        self, model, messages, temperature, max_tokens, response_model, reasoning=None, verbosity=None
    ):
        schema = response_model.model_json_schema()
        schema_str = json.dumps(schema, ensure_ascii=False)
        schema_instruction = (
            f"\n\nRespond ONLY with valid JSON matching this schema (no markdown fences):\n{schema_str}"
        )

        patched_messages = list(messages)
        if patched_messages and patched_messages[0].get("role") == "system":
            patched_messages[0] = dict(patched_messages[0])
            patched_messages[0]["content"] = patched_messages[0]["content"] + schema_instruction
        else:
            patched_messages.insert(0, {"role": "system", "content": schema_instruction})

        effective_max_tokens = max(max_tokens or 8192, 8192)

        is_reasoning = model.startswith("gpt-5") or model.startswith("o1") or model.startswith("o3")

        response = await self.client.chat.completions.create(
            model=model,
            messages=patched_messages,
            temperature=temperature if not is_reasoning else None,
            max_tokens=effective_max_tokens,
            response_format={"type": "json_object"},
            stream=False,
        )

        finish_reason = response.choices[0].finish_reason if response.choices else "unknown"
        if finish_reason == "length":
            raise Exception(f"LLM output truncated (finish_reason=length, max_tokens={effective_max_tokens})")

        return response

    async def _create_completion(
        self, model, messages, temperature, max_tokens, response_model=None, reasoning=None, verbosity=None
    ):
        """Override to add stream=False for TimiAPI compatibility."""
        is_reasoning = model.startswith("gpt-5") or model.startswith("o1") or model.startswith("o3")
        return await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature if not is_reasoning else None,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            stream=False,
        )

    def _handle_structured_response(self, response):
        content = response.choices[0].message.content or "{}"
        input_tokens = 0
        output_tokens = 0
        if hasattr(response, "usage") and response.usage:
            input_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(response.usage, "completion_tokens", 0) or 0
        return self._parse_json_robust(content), input_tokens, output_tokens

    @staticmethod
    def _parse_json_robust(text: str) -> dict:
        """Try multiple strategies to parse potentially malformed JSON."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        import re
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        cleaned = re.sub(r',\s*([}\]])', r'\1', text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        if start >= 0 and end > start:
            cleaned2 = re.sub(r',\s*([}\]])', r'\1', text[start:end])
            try:
                return json.loads(cleaned2)
            except json.JSONDecodeError:
                pass
        with open(Path(__file__).parent / "_debug_llm_output.txt", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\nFailed to parse ({len(text)} chars):\n{text[:1000]}\n{'='*60}\n")
        raise json.JSONDecodeError(f"Cannot parse JSON from LLM output ({len(text)} chars)", text, 0)

    def _handle_json_response(self, response):
        content = response.choices[0].message.content or "{}"
        input_tokens = 0
        output_tokens = 0
        if hasattr(response, "usage") and response.usage:
            input_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(response.usage, "completion_tokens", 0) or 0
        return self._parse_json_robust(content), input_tokens, output_tokens


class LocalCrossEncoder(CrossEncoderClient):
    """Uses sentence-transformers CrossEncoder for local reranking."""

    def __init__(self):
        from sentence_transformers import CrossEncoder as STCrossEncoder
        print("  Loading local cross-encoder: cross-encoder/ms-marco-MiniLM-L-6-v2...")
        self.model = STCrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        print("  Cross-encoder ready.")

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        if not passages:
            return []
        pairs = [[query, p] for p in passages]
        scores = self.model.predict(pairs)
        ranked = sorted(zip(passages, scores.tolist()), key=lambda x: x[1], reverse=True)
        return ranked


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
    MAX_EPISODES = 78
    if len(summaries) > MAX_EPISODES:
        print(f"  Limiting to first {MAX_EPISODES} of {len(summaries)} summaries (1/10)")
        summaries = summaries[:MAX_EPISODES]
    return summaries


async def run():
    print("=" * 60)
    print("Graphiti Pipeline — minusjiang_shadowfolk_export.db")
    print("=" * 60)

    # --- Load summaries ---
    print("\n[1/4] Loading session summaries...")
    summaries = load_summaries()
    print(f"  Found {len(summaries)} unique summaries")

    # --- Initialize Graphiti ---
    print("\n[2/4] Initializing Graphiti (Kuzu + Venus API)...")

    llm_config = LLMConfig(
        api_key=TIMI_TOKEN,
        base_url=TIMI_URL.replace("/chat/completions", ""),
        model=MODEL,
        small_model=SMALL_MODEL,
    )
    llm_client = TimiOpenAIClient(config=llm_config)

    embedder = LocalEmbedder()
    cross_encoder = LocalCrossEncoder()

    driver = KuzuDriver(db=KUZU_DB_PATH)
    driver._database = "default"
    driver.default_group_id = "default"

    graphiti = Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )

    await graphiti.build_indices_and_constraints()

    import kuzu as _kuzu
    _conn = _kuzu.Connection(driver.db)
    fts_queries = [
        "CALL CREATE_FTS_INDEX('Episodic', 'episode_content', ['content', 'source', 'source_description']);",
        "CALL CREATE_FTS_INDEX('Entity', 'node_name_and_summary', ['name', 'summary']);",
        "CALL CREATE_FTS_INDEX('Community', 'community_name', ['name']);",
        "CALL CREATE_FTS_INDEX('RelatesToNode_', 'edge_name_and_fact', ['name', 'fact']);",
    ]
    for q in fts_queries:
        try:
            _conn.execute(q)
        except Exception as e:
            if "already exists" not in str(e).lower():
                print(f"  FTS warning: {e}")
    _conn.close()

    print("  Graphiti ready (with FTS indices).")

    # --- Ingest episodes ---
    import time as _time
    total = len(summaries)
    print(f"\n[3/4] Ingesting {total} episodes...")
    t_start = _time.time()
    success_count = 0
    error_count = 0

    for i, s in enumerate(summaries):
        ts = datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))
        episode_name = f"{s['person_id']}_session_{s['timestamp'][:10]}_{i}"
        episode_body = f"[{s['person_id']}] {s['content']}"

        if i % 50 == 0 or i < 5:
            print(f"\n  [{i+1}/{total}] {s['person_id']} @ {s['timestamp'][:10]}")
            print(f"    Content: {s['content'][:80]}...")

        for attempt in range(5):
            try:
                result = await graphiti.add_episode(
                    name=episode_name,
                    episode_body=episode_body,
                    source_description=f"codebuddy-mem session summary by {s['person_id']}",
                    reference_time=ts,
                    source=EpisodeType.text,
                )
                success_count += 1
                if i % 50 == 0 or i < 5:
                    print(f"    -> Done. Entities: {len(result.nodes)}, Edges: {len(result.edges)}")
                break
            except Exception as e:
                err_str = str(e).lower()
                if "rate limit" in err_str or "429" in err_str:
                    wait = 30 * (attempt + 1)
                    print(f"    -> Rate limited, waiting {wait}s (attempt {attempt+1}/5)...")
                    await asyncio.sleep(wait)
                else:
                    error_count += 1
                    if i % 50 == 0 or i < 5:
                        print(f"    -> ERROR: {e}", file=sys.stderr)
                    break
        await asyncio.sleep(2)

        if (i + 1) % 10 == 0:
            elapsed = _time.time() - t_start
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"  --- Progress: {i+1}/{total} | OK:{success_count} ERR:{error_count} | "
                  f"{elapsed:.0f}s elapsed | {rate:.2f} ep/s | ETA: {eta:.0f}s ---")

    elapsed_total = _time.time() - t_start
    print(f"\n  Ingestion complete: {success_count} OK, {error_count} errors, {elapsed_total:.0f}s total")

    # --- Export graph data ---
    print(f"\n[4/4] Exporting graph data...")

    nodes_result = await driver.execute_query(
        "MATCH (n:Entity) RETURN n.uuid, n.name, n.summary, n.group_id"
    )
    edges_result = await driver.execute_query(
        "MATCH (s:Entity)-[:RELATES_TO]->(rn:RelatesToNode_)-[:RELATES_TO]->(t:Entity) "
        "RETURN s.name, t.name, rn.name, rn.fact, rn.valid_at, rn.invalid_at, rn.uuid"
    )
    episodes_result = await driver.execute_query(
        "MATCH (e:Episodic) RETURN e.uuid, e.name, e.content, e.valid_at"
    )

    print(f"  Entities: {len(nodes_result)}")
    print(f"  Edges: {len(edges_result)}")
    print(f"  Episodes: {len(episodes_result)}")

    type_colors = {
        "person": "#64e4ff", "tech": "#3a7bff", "document": "#6e5cff",
        "concept": "#ff8c42", "issue": "#ff4a6b",
    }

    nodes = []
    for row in nodes_result:
        uuid, name, summary, group_id = row[0], row[1], row[2], row[3]
        node_type = classify_node_type(name or "")

        nodes.append({
            "id": uuid or name,
            "label": name or uuid,
            "type": node_type,
            "summary": summary or "",
            "details": {"group_id": group_id or "shadowfolk"},
        })

    edges = []
    for i, row in enumerate(edges_result):
        src_name, tgt_name, rel_name, fact, valid_at, invalid_at, uuid = (
            row[0], row[1], row[2], row[3], row[4], row[5], row[6]
        )
        src_node = next((n for n in nodes if n["label"] == src_name), None)
        tgt_node = next((n for n in nodes if n["label"] == tgt_name), None)
        if not src_node or not tgt_node:
            continue
        edges.append({
            "id": uuid or f"e{i}",
            "source": src_node["id"],
            "target": tgt_node["id"],
            "relation": rel_name or "RELATES_TO",
            "fact": fact or "",
            "valid_at": str(valid_at) if valid_at else None,
            "invalid_at": str(invalid_at) if invalid_at else None,
            "episodes": [],
        })

    graph_data = {"nodes": nodes, "edges": edges}
    OUTPUT_JSON.write_text(json.dumps(graph_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"  JSON saved to {OUTPUT_JSON}")

    generate_html(graph_data)

    await graphiti.close()
    print(f"\n{'=' * 60}")
    print(f"Done! Open {OUTPUT_HTML} in a browser.")
    print(f"{'=' * 60}")


def generate_html(graph_data: dict):
    template = Path(__file__).parent.parent.parent / "graphiti-knowledge-graph-beautiful.html"
    html = template.read_text(encoding="utf-8")

    type_colors = {
        "person": "#64e4ff", "tech": "#3a7bff", "document": "#6e5cff",
        "concept": "#ff8c42", "issue": "#ff4a6b",
    }
    type_labels = {
        "person": "人物", "tech": "技术 / 工具", "document": "文档 / 文件",
        "concept": "概念 / 方案", "issue": "问题 / Bug",
    }

    used_types = set(n["type"] for n in graph_data["nodes"])
    legend_items = []
    for t in ["person", "tech", "document", "concept", "issue"]:
        if t in used_types:
            c = type_colors[t]
            legend_items.append(
                f'    <div class="item"><div class="dot" style="background:{c};color:{c}"></div>'
                f'<span class="item-label">{type_labels[t]}</span></div>'
            )
    legend_html = "\n".join(legend_items)

    nodes_json = json.dumps(graph_data["nodes"], ensure_ascii=False, indent=2, default=str)
    edges_json = json.dumps(graph_data["edges"], ensure_ascii=False, indent=2, default=str)

    style_start = html.find("<style>")
    style_end = html.find("</style>") + len("</style>")
    style_block = html[style_start:style_end]

    new_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ShadowFolk 知识图谱 — Graphiti Pipeline</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Exo+2:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400&family=Share+Tech+Mono&family=Noto+Sans+SC:wght@300;400;500;600&display=swap" rel="stylesheet">
{style_block}
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
      <marker id="arrow" viewBox="0 0 10 6" refX="28" refY="3" markerWidth="8" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,3 L0,6 Z" fill="#142240"/>
      </marker>
      <marker id="arrow-highlight" viewBox="0 0 10 6" refX="28" refY="3" markerWidth="8" markerHeight="6" orient="auto-start-reverse">
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
  </div>
  <div id="title-bar">
    <div class="main-title"><div class="logo-dot"></div>ShadowFolk Knowledge Graph</div>
    <div class="sub-title">graphiti pipeline // kuzu embedded // minusjiang_shadowfolk_export.db</div>
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

const colorMap = {json.dumps(type_colors)};
const typeLabel = {json.dumps(type_labels, ensure_ascii=False)};

function nodeRadius(d) {{
  const lc = edges.filter(e => e.source === d.id || e.target === d.id
    || (e.source.id||e.source) === d.id || (e.target.id||e.target) === d.id).length;
  return Math.max(18, 12 + lc * 3);
}}

const svg = d3.select("#graph-svg");
const container = svg.append("g");
svg.call(d3.zoom().scaleExtent([0.2,4]).on("zoom", e => container.attr("transform", e.transform)));
const width = window.innerWidth - 400, height = window.innerHeight;

const simulation = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(edges).id(d => d.id).distance(140))
  .force("charge", d3.forceManyBody().strength(-400))
  .force("center", d3.forceCenter(width/2, height/2))
  .force("collision", d3.forceCollide().radius(d => nodeRadius(d)+16));

(function computeCurvatures() {{
  const pc = {{}}, pi = {{}};
  edges.forEach(e => {{
    const k = [e.source.id||e.source, e.target.id||e.target].sort().join("||");
    pc[k] = (pc[k]||0)+1; pi[k] = 0;
  }});
  edges.forEach(e => {{
    const k = [e.source.id||e.source, e.target.id||e.target].sort().join("||");
    const t = pc[k], i = pi[k]++;
    e._curve = t===1 ? 0 : (i-(t-1)/2)*45;
  }});
}})();

const linkGroup = container.append("g");
const linkPaths = linkGroup.selectAll("path").data(edges).join("path")
  .attr("fill","none").attr("stroke",d=>d.invalid_at?"#0c1628":"#142240")
  .attr("stroke-width",d=>d.invalid_at?1:1.5).attr("stroke-dasharray",d=>d.invalid_at?"6 4":"none")
  .attr("marker-end","url(#arrow)").attr("cursor","pointer")
  .on("click",(ev,d)=>{{ev.stopPropagation();showEdgeDetail(d)}})
  .on("mouseenter",(ev,d)=>highlightEdge(d,true)).on("mouseleave",(ev,d)=>highlightEdge(d,false));

const linkLabels = linkGroup.selectAll("text").data(edges).join("text")
  .text(d=>d.relation).attr("font-size",8).attr("font-family","'Share Tech Mono',monospace")
  .attr("fill",d=>d.invalid_at?"#0e1830":"#2a4068").attr("text-anchor","middle").attr("dy",-8)
  .attr("cursor","pointer").attr("letter-spacing","1px")
  .on("click",(ev,d)=>{{ev.stopPropagation();showEdgeDetail(d)}});

const nodeGroup = container.append("g");
const nodeEls = nodeGroup.selectAll("g").data(nodes).join("g").attr("cursor","grab")
  .call(d3.drag()
    .on("start",(ev,d)=>{{if(!ev.active)simulation.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y}})
    .on("drag",(ev,d)=>{{d.fx=ev.x;d.fy=ev.y}})
    .on("end",(ev,d)=>{{if(!ev.active)simulation.alphaTarget(0);d.fx=null;d.fy=null}}));

nodeEls.append("circle").attr("r",d=>nodeRadius(d)+8).attr("fill","none")
  .attr("stroke",d=>colorMap[d.type]||"#888").attr("stroke-width",0.5).attr("stroke-opacity",0.12).attr("stroke-dasharray","4 3");
nodeEls.append("circle").attr("class","node-main").attr("r",d=>nodeRadius(d))
  .attr("fill",d=>colorMap[d.type]||"#888").attr("fill-opacity",0.06)
  .attr("stroke",d=>colorMap[d.type]||"#888").attr("stroke-width",1.5).attr("stroke-opacity",0.7).attr("filter","url(#glow)");
nodeEls.append("circle").attr("r",d=>nodeRadius(d)*0.22)
  .attr("fill",d=>colorMap[d.type]||"#888").attr("fill-opacity",0.6).attr("filter","url(#glow)");
nodeEls.append("text").text(d=>d.label).attr("text-anchor","middle").attr("dy",d=>nodeRadius(d)+20)
  .attr("fill","#4a6890").attr("font-size",10).attr("font-weight",500)
  .attr("font-family","'Exo 2','Noto Sans SC',sans-serif").attr("letter-spacing","0.5px");

nodeEls.on("click",(ev,d)=>{{ev.stopPropagation();showNodeDetail(d)}})
  .on("mouseenter",(ev,d)=>highlightNode(d,true)).on("mouseleave",(ev,d)=>highlightNode(d,false));

simulation.on("tick",()=>{{
  linkPaths.attr("d",d=>{{
    const dx=d.target.x-d.source.x,dy=d.target.y-d.source.y;
    if(d._curve===0)return`M${{d.source.x}},${{d.source.y}} L${{d.target.x}},${{d.target.y}}`;
    const mx=(d.source.x+d.target.x)/2,my=(d.source.y+d.target.y)/2,len=Math.sqrt(dx*dx+dy*dy)||1;
    return`M${{d.source.x}},${{d.source.y}} Q${{mx+(-dy/len*d._curve)}},${{my+(dx/len*d._curve)}} ${{d.target.x}},${{d.target.y}}`;
  }});
  linkLabels.attr("x",d=>{{const mx=(d.source.x+d.target.x)/2;if(d._curve===0)return mx;const dx=d.target.x-d.source.x,dy=d.target.y-d.source.y,len=Math.sqrt(dx*dx+dy*dy)||1;return mx+(-dy/len*d._curve)*0.5}})
    .attr("y",d=>{{const my=(d.source.y+d.target.y)/2;if(d._curve===0)return my;const dx=d.target.x-d.source.x,dy=d.target.y-d.source.y,len=Math.sqrt(dx*dx+dy*dy)||1;return my+(dx/len*d._curve)*0.5}});
  nodeEls.attr("transform",d=>`translate(${{d.x}},${{d.y}})`);
}});

function highlightNode(d,on){{
  const ce=new Set(),cn=new Set([d.id]);
  edges.forEach(e=>{{const s=e.source.id||e.source,t=e.target.id||e.target;if(s===d.id||t===d.id){{ce.add(e.id);cn.add(s);cn.add(t)}}}});
  if(on){{
    nodeEls.select(".node-main").transition().duration(200).attr("fill-opacity",n=>cn.has(n.id)?0.15:0.015).attr("stroke-opacity",n=>cn.has(n.id)?1:0.06).attr("filter",n=>cn.has(n.id)?"url(#glow-strong)":"none");
    nodeEls.select("text").transition().duration(200).attr("fill-opacity",n=>cn.has(n.id)?1:0.1).attr("fill",n=>n.id===d.id?"#fff":(cn.has(n.id)?"#6890b8":"#4a6890"));
    linkPaths.transition().duration(200).attr("stroke",e=>ce.has(e.id)?"#00c8ff":"#060e1a").attr("stroke-opacity",e=>ce.has(e.id)?0.9:0.06).attr("stroke-width",e=>ce.has(e.id)?2.5:1).attr("marker-end",e=>ce.has(e.id)?"url(#arrow-highlight)":"url(#arrow)");
    linkLabels.transition().duration(200).attr("fill-opacity",e=>ce.has(e.id)?1:0.03);
  }}else{{
    nodeEls.select(".node-main").transition().duration(300).attr("fill-opacity",0.06).attr("stroke-opacity",0.7).attr("filter","url(#glow)");
    nodeEls.select("text").transition().duration(300).attr("fill-opacity",1).attr("fill","#4a6890");
    linkPaths.transition().duration(300).attr("stroke",e=>e.invalid_at?"#0c1628":"#142240").attr("stroke-opacity",1).attr("stroke-width",e=>e.invalid_at?1:1.5).attr("marker-end","url(#arrow)");
    linkLabels.transition().duration(300).attr("fill-opacity",1);
  }}
}}
function highlightEdge(e,on){{
  if(on){{linkPaths.transition().duration(150).attr("stroke",l=>l.id===e.id?"#00c8ff":"#060e1a").attr("stroke-opacity",l=>l.id===e.id?1:0.1).attr("stroke-width",l=>l.id===e.id?3:1);linkLabels.transition().duration(150).attr("fill-opacity",l=>l.id===e.id?1:0.06).attr("fill",l=>l.id===e.id?"#64e4ff":"#2a4068")}}
  else{{linkPaths.transition().duration(250).attr("stroke",l=>l.invalid_at?"#0c1628":"#142240").attr("stroke-opacity",1).attr("stroke-width",l=>l.invalid_at?1:1.5);linkLabels.transition().duration(250).attr("fill-opacity",1).attr("fill",l=>l.invalid_at?"#0e1830":"#2a4068")}}
}}

const panel=d3.select("#detail-panel"),panelContent=d3.select("#panel-content");
d3.select("#close-panel").on("click",()=>panel.classed("hidden",true));
svg.on("click",()=>panel.classed("hidden",true));

function showNodeDetail(d){{
  const c=colorMap[d.type]||"#888";
  const re=edges.filter(e=>(e.source.id||e.source)===d.id||(e.target.id||e.target)===d.id);
  let h=`<h2>${{d.label}}</h2><span class="type-badge" style="background:${{c}}18;color:${{c}};border:1px solid ${{c}}35">${{typeLabel[d.type]||d.type}}</span>`;
  h+=`<div class="section-title">摘要 Summary</div><div class="fact-text" style="border-left-color:${{c}}">${{d.summary||'暂无摘要'}}</div>`;
  if(d.details){{h+=`<div class="section-title">属性 Properties</div>`;Object.entries(d.details).forEach(([k,v])=>{{h+=`<div class="meta-row"><span class="label">${{k}}</span><span class="value">${{v}}</span></div>`}})}}
  h+=`<div class="section-title">关联事实边 (${{re.length}})</div>`;
  re.forEach(e=>{{const s=e.source.id||e.source,t=e.target.id||e.target,dir=s===d.id?`→ ${{t}}`:`← ${{s}}`;h+=`<div class="edge-list-item" data-edge-id="${{e.id}}"><span class="rel">${{e.relation}}</span><span class="target">${{dir}}</span><div style="margin-top:6px;color:#3a5078;font-size:12px;line-height:1.6">${{e.fact}}</div></div>`}});
  panelContent.html(h);panel.classed("hidden",false);
  panelContent.selectAll(".edge-list-item").on("click",function(){{const edge=edges.find(e=>e.id===this.getAttribute("data-edge-id"));if(edge)showEdgeDetail(edge)}});
}}
function showEdgeDetail(e){{
  const s=e.source.id||e.source,t=e.target.id||e.target;
  let h=`<h2 style="font-size:16px;color:#64e4ff">${{s}} → ${{t}}</h2><span class="type-badge" style="background:#00c8ff18;color:#64e4ff;border:1px solid #00c8ff35">${{e.relation}}</span>`;
  if(e.invalid_at)h+=`<span class="type-badge" style="background:#ff4a4a12;color:#ff6b6b;border:1px solid #ff4a4a30;margin-left:6px">已失效</span>`;
  h+=`<div class="section-title">事实 Fact</div><div class="fact-text">${{e.fact}}</div>`;
  h+=`<div class="section-title">时间信息</div><div class="meta-row"><span class="label">valid_at</span><span class="value">${{e.valid_at||'—'}}</span></div><div class="meta-row"><span class="label">invalid_at</span><span class="value">${{e.invalid_at||'<span style="color:#00c8ff">null (仍有效)</span>'}}</span></div>`;
  h+=`<div class="section-title">关联节点</div><div class="edge-list-item" data-node-id="${{s}}"><span class="rel">Source</span><span class="target">${{s}}</span></div><div class="edge-list-item" data-node-id="${{t}}"><span class="rel">Target</span><span class="target">${{t}}</span></div>`;
  panelContent.html(h);panel.classed("hidden",false);
  panelContent.selectAll(".edge-list-item[data-node-id]").on("click",function(){{const n=nodes.find(n=>n.id===this.getAttribute("data-node-id"));if(n)showNodeDetail(n)}});
}}

window.addEventListener("resize",()=>{{simulation.force("center",d3.forceCenter(window.innerWidth/2,window.innerHeight/2));simulation.alpha(0.3).restart()}});
</script>
</body>
</html>"""

    OUTPUT_HTML.write_text(new_html, encoding="utf-8")
    print(f"  HTML written to {OUTPUT_HTML}")


async def export_only():
    """Only export data from existing Kuzu DB, skip ingestion."""
    print("=" * 60)
    print("Export Only — reading from existing Kuzu DB")
    print("=" * 60)

    driver = KuzuDriver(db=KUZU_DB_PATH)

    print("\nQuerying graph data...")
    nodes_raw, _, _ = await driver.execute_query(
        "MATCH (n:Entity) RETURN n.uuid, n.name, n.summary, n.group_id"
    )
    edges_raw, _, _ = await driver.execute_query(
        "MATCH (s:Entity)-[:RELATES_TO]->(rn:RelatesToNode_)-[:RELATES_TO]->(t:Entity) "
        "RETURN s.name AS src, t.name AS tgt, rn.name AS rel, rn.fact AS fact, "
        "rn.valid_at AS valid_at, rn.invalid_at AS invalid_at, rn.uuid AS uuid"
    )
    episodes_raw, _, _ = await driver.execute_query(
        "MATCH (e:Episodic) RETURN e.uuid, e.name, e.content, e.valid_at"
    )

    print(f"  Entities: {len(nodes_raw)}")
    print(f"  Edges: {len(edges_raw)}")
    print(f"  Episodes: {len(episodes_raw)}")

    nodes = []
    for row in nodes_raw:
        uuid = row.get("n.uuid", "")
        name = row.get("n.name", "")
        summary = row.get("n.summary", "")
        group_id = row.get("n.group_id", "")
        node_type = classify_node_type(name)
        nodes.append({
            "id": uuid or name,
            "label": name or uuid,
            "type": node_type,
            "summary": summary or "",
            "details": {"group_id": group_id or "shadowfolk"},
        })

    edges = []
    for i, row in enumerate(edges_raw):
        src_name = row.get("src", "")
        tgt_name = row.get("tgt", "")
        rel_name = row.get("rel", "")
        fact = row.get("fact", "")
        valid_at = row.get("valid_at")
        invalid_at = row.get("invalid_at")
        uuid = row.get("uuid", "")

        src_node = next((n for n in nodes if n["label"] == src_name), None)
        tgt_node = next((n for n in nodes if n["label"] == tgt_name), None)
        if not src_node or not tgt_node:
            continue
        edges.append({
            "id": uuid or f"e{i}",
            "source": src_node["id"],
            "target": tgt_node["id"],
            "relation": rel_name or "RELATES_TO",
            "fact": fact or "",
            "valid_at": str(valid_at) if valid_at else None,
            "invalid_at": str(invalid_at) if invalid_at else None,
            "episodes": [],
        })

    graph_data = {"nodes": nodes, "edges": edges}
    OUTPUT_JSON.write_text(json.dumps(graph_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"  JSON saved to {OUTPUT_JSON}")

    generate_html(graph_data)

    await driver.close()
    print(f"\n{'=' * 60}")
    print(f"Done! Open {OUTPUT_HTML} in a browser.")
    print(f"{'=' * 60}")


def classify_node_type(name: str) -> str:
    name_lower = name.lower()
    if name_lower in ["minusjiang", "用户", "user"]:
        return "person"
    elif any(kw in name_lower for kw in [".py", ".ts", ".js", ".css", ".html", ".csv", ".ini", ".yaml", ".json", ".md", ".sql", "文档", "文件", "报告", "脚本"]):
        return "document"
    elif any(kw in name_lower for kw in ["问题", "异常", "错误", "失败", "崩溃", "bug", "issue", "error"]):
        return "issue"
    elif any(kw in name_lower for kw in [
        "python", "flask", "fastapi", "sqlite", "redis", "neo4j", "docker",
        "mcp", "api", "cli", "git", "svn", "react", "vue", "typescript",
        "graphiti", "mem0", "shadow", "kuzu", "embedding", "llm",
    ]):
        return "tech"
    return "concept"


if __name__ == "__main__":
    import sys as _sys
    if "--export-only" in _sys.argv:
        asyncio.run(export_only())
    else:
        asyncio.run(run())
