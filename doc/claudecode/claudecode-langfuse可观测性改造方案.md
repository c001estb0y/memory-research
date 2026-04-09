# Claude Code + Langfuse 可观测性改造方案

基于 Langfuse 官方 Claude Code 集成的增强版，补全 token 用量、成本追踪和缓存命中率监控。

---

## 一、背景：官方方案缺了什么

Langfuse 官方提供了 [Claude Code 集成](https://langfuse.com/integrations/other/claude-code)，通过 Claude Code 的 hooks 机制 + transcript 文件实现旁路观测。但官方脚本有一个关键缺失：**没有提取 transcript 中的 token 用量数据**。

### 1.1 数据已经存在，只是没被读取

Claude Code 的 transcript（`.jsonl`）中，每条 `assistant` 消息都包含完整的 API usage 数据：

```json
{
  "type": "assistant",
  "message": {
    "role": "assistant",
    "model": "claude-4.6-opus",
    "content": [...],
    "stop_reason": "end_turn",
    "usage": {
      "input_tokens": 176337,
      "cache_creation_input_tokens": 836,
      "cache_read_input_tokens": 175500,
      "output_tokens": 783,
      "service_tier": "standard"
    }
  }
}
```

### 1.2 官方 hook 脚本的 emit_turn 函数只传了 model、input、output

```python
# 官方原版——没有 usage 参数
with langfuse.start_as_current_observation(
    name="Claude Response",
    as_type="generation",
    model=model,
    input={"role": "user", "content": user_text},
    output={"role": "assistant", "content": assistant_text},
    metadata={...},
):
    pass
```

### 1.3 改造后能看到什么

| 监控维度 | 官方原版 | 改造后 |
|---------|---------|-------|
| 用户输入文本 | ✅ | ✅ |
| 模型输出文本 | ✅ | ✅ |
| 工具调用链（tool_use → tool_result） | ✅ | ✅ |
| **每轮 input_tokens** | ❌ | ✅ |
| **每轮 output_tokens** | ❌ | ✅ |
| **成本自动计算（USD）** | ❌ | ✅（Langfuse 根据 model + tokens 自动算） |
| **cache_read_input_tokens** | ❌ | ✅（metadata） |
| **cache_creation_input_tokens** | ❌ | ✅（metadata） |
| **缓存命中率** | ❌ | ✅（metadata 中可计算） |
| **stop_reason** | ❌ | ✅（metadata） |
| **thinking 内容** | ❌ | ✅ |
| **多 assistant 消息的聚合 usage** | ❌ | ✅（累加每轮所有 assistant 消息的 tokens） |

---

## 二、架构总览

```
Claude Code 运行（零改动）
    │
    ├─ 1. 自动写 transcript (.jsonl)
    │     路径: ~/.claude/projects/<project>/<session-id>.jsonl
    │     内容: 每条消息一行 JSON，assistant 消息含完整 usage
    │
    └─ 2. 每轮回复结束 → 触发 Stop hook
          │
          └─ 3. langfuse_hook.py 被调用（stdin 接收 session_id + transcript_path）
                │
                ├─ 增量读取 transcript（从上次 offset 继续，不重复处理）
                ├─ 解析成 Turn（user → assistant[] → tool_result[]）
                ├─ ★ 提取每个 assistant 消息的 usage 数据
                ├─ ★ 聚合多条 assistant 消息的 token 总量
                ├─ ★ 提取 thinking 内容
                └─ 推送到 Langfuse（generation 带 usage 参数）
                    │
                    └─ Langfuse 自动：
                       ├─ 根据 model + tokens 计算 USD 成本
                       ├─ 按 session_id 分组会话
                       └─ Dashboard 展示 token 趋势、成本趋势
```

---

## 三、完整改造后的 Hook 脚本

将以下文件放置到 `~/.claude/hooks/langfuse_hook.py`：

```python
#!/usr/bin/env python3
"""
Claude Code -> Langfuse hook (增强版)

基于 Langfuse 官方集成改造，补全：
- 每轮 input_tokens / output_tokens 追踪
- cache_read / cache_creation token 追踪
- 成本自动计算（Langfuse 根据 model + tokens 自动计算 USD）
- thinking 内容提取
- stop_reason 追踪
- 多 assistant 消息的 usage 聚合

使用方式：
  1. 放到 ~/.claude/hooks/langfuse_hook.py
  2. 在 ~/.claude/settings.json 注册 Stop hook
  3. 在项目 .claude/settings.local.json 配置 Langfuse 密钥
"""

import json
import os
import sys
import time
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from langfuse import Langfuse, propagate_attributes
except Exception:
    sys.exit(0)

# --- 配置 ---
STATE_DIR = Path.home() / ".claude" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"

DEBUG = os.environ.get("CC_LANGFUSE_DEBUG", "").lower() == "true"
MAX_CHARS = int(os.environ.get("CC_LANGFUSE_MAX_CHARS", "20000"))


# ─── 日志 ──────────────────────────────────────────────

def _log(level: str, message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {message}\n")
    except Exception:
        pass

def debug(msg: str) -> None:
    if DEBUG:
        _log("DEBUG", msg)

def info(msg: str) -> None:
    _log("INFO", msg)


# ─── 文件锁（并发安全） ──────────────────────────────────

class FileLock:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl
            deadline = time.time() + self.timeout_s
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        break
                    time.sleep(0.05)
        except ImportError:
            # Windows: fcntl 不可用，跳过文件锁
            try:
                import msvcrt
                deadline = time.time() + self.timeout_s
                while True:
                    try:
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except (IOError, OSError):
                        if time.time() > deadline:
                            break
                        time.sleep(0.05)
            except Exception:
                pass
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


# ─── 状态持久化（增量读取） ──────────────────────────────

def load_state() -> Dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        debug(f"save_state failed: {e}")

def state_key(session_id: str, transcript_path: str) -> str:
    raw = f"{session_id}::{transcript_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ─── Hook payload 解析 ──────────────────────────────────

def read_hook_payload() -> Dict[str, Any]:
    try:
        data = sys.stdin.read()
        if not data.strip():
            return {}
        return json.loads(data)
    except Exception:
        return {}

def extract_session_and_transcript(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[Path]]:
    session_id = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("session", {}).get("id")
    )
    transcript = (
        payload.get("transcriptPath")
        or payload.get("transcript_path")
        or payload.get("transcript", {}).get("path")
    )
    transcript_path = None
    if transcript:
        try:
            transcript_path = Path(transcript).expanduser().resolve()
        except Exception:
            pass
    return session_id, transcript_path


# ─── Transcript 解析 helpers ────────────────────────────

def get_content(msg: Dict[str, Any]) -> Any:
    if not isinstance(msg, dict):
        return None
    if "message" in msg and isinstance(msg.get("message"), dict):
        return msg["message"].get("content")
    return msg.get("content")

def get_role(msg: Dict[str, Any]) -> Optional[str]:
    t = msg.get("type")
    if t in ("user", "assistant"):
        return t
    m = msg.get("message")
    if isinstance(m, dict):
        r = m.get("role")
        if r in ("user", "assistant"):
            return r
    return None

def is_tool_result(msg: Dict[str, Any]) -> bool:
    if get_role(msg) != "user":
        return False
    content = get_content(msg)
    if isinstance(content, list):
        return any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
    return False

def iter_tool_results(content: Any) -> List[Dict[str, Any]]:
    out = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                out.append(x)
    return out

def iter_tool_uses(content: Any) -> List[Dict[str, Any]]:
    out = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                out.append(x)
    return out

def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join([p for p in parts if p])
    return ""

def truncate_text(s: str, max_chars: int = MAX_CHARS) -> Tuple[str, Dict[str, Any]]:
    if s is None:
        return "", {"truncated": False, "orig_len": 0}
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {
        "truncated": True,
        "orig_len": orig_len,
        "kept_len": len(head),
        "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest(),
    }

def get_model(msg: Dict[str, Any]) -> str:
    m = msg.get("message")
    if isinstance(m, dict):
        return m.get("model") or "claude"
    return "claude"

def get_message_id(msg: Dict[str, Any]) -> Optional[str]:
    m = msg.get("message")
    if isinstance(m, dict):
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            return mid
    return None


# ─── ★ 新增：usage / thinking / stop_reason 提取 ──────

def get_usage(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从单条 assistant 消息中提取 API 返回的 usage 数据"""
    m = msg.get("message")
    if isinstance(m, dict):
        return m.get("usage")
    return None

def get_stop_reason(msg: Dict[str, Any]) -> Optional[str]:
    """从 assistant 消息中提取 stop_reason"""
    m = msg.get("message")
    if isinstance(m, dict):
        return m.get("stop_reason")
    return None

def extract_thinking(content: Any) -> str:
    """从 content 数组中提取 thinking block 的内容"""
    if not isinstance(content, list):
        return ""
    parts = []
    for x in content:
        if isinstance(x, dict) and x.get("type") == "thinking":
            thinking_text = x.get("thinking", "")
            if thinking_text:
                parts.append(thinking_text)
    return "\n---\n".join(parts)

def aggregate_usage(assistant_msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    聚合一个 turn 中所有 assistant 消息的 usage。
    
    一个 turn 可能有多条 assistant 消息（模型回复 → 工具调用 → 再回复 → 再调用...），
    每条都有自己的 usage。这里将它们累加，同时记录每条的明细。
    
    注意：input_tokens 不应简单相加（因为后续调用包含前面的 context），
    所以 input_tokens 取最后一条的值（最完整），output_tokens 则累加。
    """
    total_output = 0
    total_cache_creation = 0
    total_cache_read = 0
    last_input = 0
    per_message_usage = []

    for am in assistant_msgs:
        u = get_usage(am)
        if not u:
            continue
        
        input_t = u.get("input_tokens", 0)
        output_t = u.get("output_tokens", 0)
        cache_create = u.get("cache_creation_input_tokens", 0)
        cache_read = u.get("cache_read_input_tokens", 0)
        
        last_input = input_t
        total_output += output_t
        total_cache_creation += cache_create
        total_cache_read += cache_read
        
        per_message_usage.append({
            "message_id": get_message_id(am),
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
        })

    cache_hit_rate = 0.0
    if last_input > 0:
        cache_hit_rate = round(total_cache_read / last_input * 100, 2)

    return {
        "input_tokens": last_input,
        "output_tokens": total_output,
        "cache_creation_input_tokens": total_cache_creation,
        "cache_read_input_tokens": total_cache_read,
        "cache_hit_rate_percent": cache_hit_rate,
        "assistant_message_count": len(per_message_usage),
        "per_message_detail": per_message_usage,
    }


# ─── 增量读取 transcript ────────────────────────────────

@dataclass
class SessionState:
    offset: int = 0
    buffer: str = ""
    turn_count: int = 0

def load_session_state(global_state: Dict[str, Any], key: str) -> SessionState:
    s = global_state.get(key, {})
    return SessionState(
        offset=int(s.get("offset", 0)),
        buffer=str(s.get("buffer", "")),
        turn_count=int(s.get("turn_count", 0)),
    )

def write_session_state(global_state: Dict[str, Any], key: str, ss: SessionState) -> None:
    global_state[key] = {
        "offset": ss.offset,
        "buffer": ss.buffer,
        "turn_count": ss.turn_count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

def read_new_jsonl(transcript_path: Path, ss: SessionState) -> Tuple[List[Dict[str, Any]], SessionState]:
    if not transcript_path.exists():
        return [], ss
    try:
        with open(transcript_path, "rb") as f:
            f.seek(ss.offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception as e:
        debug(f"read_new_jsonl failed: {e}")
        return [], ss
    if not chunk:
        return [], ss
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = chunk.decode(errors="replace")
    combined = ss.buffer + text
    lines = combined.split("\n")
    ss.buffer = lines[-1]
    ss.offset = new_offset
    msgs = []
    for line in lines[:-1]:
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except Exception:
            continue
    return msgs, ss


# ─── Turn 组装 ──────────────────────────────────────────

@dataclass
class Turn:
    user_msg: Dict[str, Any]
    assistant_msgs: List[Dict[str, Any]]
    tool_results_by_id: Dict[str, Any]

def build_turns(messages: List[Dict[str, Any]]) -> List[Turn]:
    turns: List[Turn] = []
    current_user: Optional[Dict[str, Any]] = None
    assistant_order: List[str] = []
    assistant_latest: Dict[str, Dict[str, Any]] = {}
    tool_results_by_id: Dict[str, Any] = {}

    def flush_turn():
        nonlocal current_user, assistant_order, assistant_latest, tool_results_by_id
        if current_user is None or not assistant_latest:
            return
        assistants = [assistant_latest[mid] for mid in assistant_order if mid in assistant_latest]
        turns.append(Turn(
            user_msg=current_user,
            assistant_msgs=assistants,
            tool_results_by_id=dict(tool_results_by_id),
        ))

    for msg in messages:
        role = get_role(msg)

        if is_tool_result(msg):
            for tr in iter_tool_results(get_content(msg)):
                tid = tr.get("tool_use_id")
                if tid:
                    tool_results_by_id[str(tid)] = tr.get("content")
            continue

        if role == "user":
            flush_turn()
            current_user = msg
            assistant_order = []
            assistant_latest = {}
            tool_results_by_id = {}
            continue

        if role == "assistant":
            if current_user is None:
                continue
            mid = get_message_id(msg) or f"noid:{len(assistant_order)}"
            if mid not in assistant_latest:
                assistant_order.append(mid)
            assistant_latest[mid] = msg
            continue

    flush_turn()
    return turns


# ─── ★ 改造后的 Langfuse 推送 ──────────────────────────

def _tool_calls_from_assistants(assistant_msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    calls = []
    for am in assistant_msgs:
        for tu in iter_tool_uses(get_content(am)):
            tid = tu.get("id") or ""
            calls.append({
                "id": str(tid),
                "name": tu.get("name") or "unknown",
                "input": tu.get("input") if isinstance(tu.get("input"), (dict, list, str, int, float, bool)) else {},
            })
    return calls

def emit_turn(
    langfuse: Langfuse,
    session_id: str,
    turn_num: int,
    turn: Turn,
    transcript_path: Path,
) -> None:
    # --- 提取用户输入 ---
    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw)

    # --- 提取模型输出 ---
    last_assistant = turn.assistant_msgs[-1]
    assistant_text_raw = extract_text(get_content(last_assistant))
    assistant_text, assistant_text_meta = truncate_text(assistant_text_raw)

    # --- 提取模型名 ---
    model = get_model(turn.assistant_msgs[0])

    # --- ★ 提取 usage（改造核心） ---
    usage_agg = aggregate_usage(turn.assistant_msgs)
    
    usage_param = None
    if usage_agg["input_tokens"] > 0 or usage_agg["output_tokens"] > 0:
        usage_param = {
            "input": usage_agg["input_tokens"],
            "output": usage_agg["output_tokens"],
            "unit": "TOKENS",
        }

    # --- ★ 提取 thinking ---
    thinking_parts = []
    for am in turn.assistant_msgs:
        t = extract_thinking(get_content(am))
        if t:
            thinking_parts.append(t)
    thinking_text = "\n===\n".join(thinking_parts) if thinking_parts else None
    thinking_trunc = None
    if thinking_text:
        thinking_text, thinking_trunc = truncate_text(thinking_text, MAX_CHARS // 2)

    # --- ★ 提取 stop_reason ---
    stop_reason = get_stop_reason(last_assistant)

    # --- 提取工具调用 ---
    tool_calls = _tool_calls_from_assistants(turn.assistant_msgs)

    for c in tool_calls:
        if c["id"] and c["id"] in turn.tool_results_by_id:
            out_raw = turn.tool_results_by_id[c["id"]]
            out_str = out_raw if isinstance(out_raw, str) else json.dumps(out_raw, ensure_ascii=False)
            out_trunc, out_meta = truncate_text(out_str)
            c["output"] = out_trunc
            c["output_meta"] = out_meta
        else:
            c["output"] = None

    # --- 推送到 Langfuse ---
    with propagate_attributes(
        session_id=session_id,
        trace_name=f"Claude Code - Turn {turn_num}",
        tags=["claude-code"],
    ):
        with langfuse.start_as_current_observation(
            name=f"Claude Code - Turn {turn_num}",
            input={"role": "user", "content": user_text},
            metadata={
                "source": "claude-code",
                "session_id": session_id,
                "turn_number": turn_num,
                "transcript_path": str(transcript_path),
                "user_text": user_text_meta,
            },
        ) as trace_span:

            # ★ LLM Generation（带 usage）
            with langfuse.start_as_current_observation(
                name="Claude Response",
                as_type="generation",
                model=model,
                input={"role": "user", "content": user_text},
                output={"role": "assistant", "content": assistant_text},
                usage=usage_param,
                metadata={
                    "assistant_text": assistant_text_meta,
                    "tool_count": len(tool_calls),
                    "stop_reason": stop_reason,
                    # ★ 缓存追踪
                    "input_tokens": usage_agg["input_tokens"],
                    "output_tokens": usage_agg["output_tokens"],
                    "cache_creation_input_tokens": usage_agg["cache_creation_input_tokens"],
                    "cache_read_input_tokens": usage_agg["cache_read_input_tokens"],
                    "cache_hit_rate_percent": usage_agg["cache_hit_rate_percent"],
                    "assistant_message_count": usage_agg["assistant_message_count"],
                    # ★ Thinking
                    "has_thinking": thinking_text is not None,
                    "thinking_length": len(thinking_text) if thinking_text else 0,
                },
            ):
                pass

            # ★ Thinking Span（如果有 thinking 内容，单独记录）
            if thinking_text:
                with langfuse.start_as_current_observation(
                    name="Thinking",
                    input={"type": "extended_thinking"},
                    metadata={
                        "thinking_truncation": thinking_trunc,
                    },
                ) as thinking_obs:
                    thinking_obs.update(output=thinking_text)

            # Tool Spans
            for tc in tool_calls:
                in_obj = tc["input"]
                in_meta = None
                if isinstance(in_obj, str):
                    in_obj, in_meta = truncate_text(in_obj)

                with langfuse.start_as_current_observation(
                    name=f"Tool: {tc['name']}",
                    as_type="tool",
                    input=in_obj,
                    metadata={
                        "tool_name": tc["name"],
                        "tool_id": tc["id"],
                        "input_meta": in_meta,
                        "output_meta": tc.get("output_meta"),
                    },
                ) as tool_obs:
                    tool_obs.update(output=tc.get("output"))

            trace_span.update(output={"role": "assistant", "content": assistant_text})


# ─── Main ──────────────────────────────────────────────

def main() -> int:
    start = time.time()
    debug("Hook started")

    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
        return 0

    public_key = os.environ.get("CC_LANGFUSE_PUBLIC_KEY") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("CC_LANGFUSE_SECRET_KEY") or os.environ.get("LANGFUSE_SECRET_KEY")
    host = (
        os.environ.get("CC_LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    )

    if not public_key or not secret_key:
        return 0

    payload = read_hook_payload()
    session_id, transcript_path = extract_session_and_transcript(payload)

    if not session_id or not transcript_path:
        debug("Missing session_id or transcript_path; exiting.")
        return 0

    if not transcript_path.exists():
        debug(f"Transcript not found: {transcript_path}")
        return 0

    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return 0

    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            key = state_key(session_id, str(transcript_path))
            ss = load_session_state(state, key)

            msgs, ss = read_new_jsonl(transcript_path, ss)
            if not msgs:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            turns = build_turns(msgs)
            if not turns:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            emitted = 0
            for t in turns:
                emitted += 1
                turn_num = ss.turn_count + emitted
                try:
                    emit_turn(langfuse, session_id, turn_num, t, transcript_path)
                except Exception as e:
                    debug(f"emit_turn failed: {e}")

            ss.turn_count += emitted
            write_session_state(state, key, ss)
            save_state(state)

        try:
            langfuse.flush()
        except Exception:
            pass

        dur = time.time() - start
        info(f"Processed {emitted} turns in {dur:.2f}s (session={session_id})")
        return 0

    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0

    finally:
        try:
            langfuse.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())
```

---

## 四、安装与配置步骤

### 4.1 前提条件

- Claude Code CLI 已安装（任意版本，hooks 和 transcript 是默认特性）
- Python 3.8+
- Langfuse 账号（[云版](https://cloud.langfuse.com/) 或自建）

### 4.2 Step 1：安装 Langfuse SDK

```bash
pip install langfuse
```

### 4.3 Step 2：放置 Hook 脚本

```bash
# macOS / Linux
mkdir -p ~/.claude/hooks
# 将上面的脚本保存为 ~/.claude/hooks/langfuse_hook.py
chmod +x ~/.claude/hooks/langfuse_hook.py
```

```powershell
# Windows (PowerShell)
New-Item -ItemType Directory -Path "$env:USERPROFILE\.claude\hooks" -Force
# 将脚本保存为 %USERPROFILE%\.claude\hooks\langfuse_hook.py
```

### 4.4 Step 3：注册全局 Hook

编辑 `~/.claude/settings.json`，添加 Stop hook：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/langfuse_hook.py"
          }
        ]
      }
    ]
  }
}
```

> **Windows 用户**：将 `python3` 替换为 `python` 或 Python 的完整路径。

### 4.5 Step 4：按项目启用 Langfuse 追踪

在需要监控的项目根目录下创建 `.claude/settings.local.json`：

```json
{
  "env": {
    "TRACE_TO_LANGFUSE": "true",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-你的公钥",
    "LANGFUSE_SECRET_KEY": "sk-lf-你的私钥",
    "LANGFUSE_BASE_URL": "https://cloud.langfuse.com"
  }
}
```

| 变量 | 说明 | 必须 |
|-----|------|-----|
| `TRACE_TO_LANGFUSE` | 设为 `"true"` 启用 | 是 |
| `LANGFUSE_PUBLIC_KEY` | Langfuse 公钥（`pk-lf-` 开头） | 是 |
| `LANGFUSE_SECRET_KEY` | Langfuse 私钥（`sk-lf-` 开头） | 是 |
| `LANGFUSE_BASE_URL` | Langfuse 地址（EU: `https://cloud.langfuse.com`，US: `https://us.cloud.langfuse.com`） | 否 |
| `CC_LANGFUSE_DEBUG` | 设为 `"true"` 开启详细日志 | 否 |
| `CC_LANGFUSE_MAX_CHARS` | 文本截断长度，默认 20000 | 否 |

### 4.6 Step 5：验证

```bash
# 启动 Claude Code
cd your-project
claude

# 进行一轮对话后，检查日志
cat ~/.claude/state/langfuse_hook.log
# 应该看到类似：
# 2026-04-08 20:15:30 [INFO] Processed 1 turns in 0.45s (session=abc123...)
```

然后打开 Langfuse Dashboard 查看 trace。

---

## 五、Langfuse 中看到的效果

### 5.1 Trace 视图

每轮对话（user → assistant）生成一个 Trace，结构如下：

```
📋 Claude Code - Turn 1                    [Trace]
├── 🤖 Claude Response                     [Generation]  ← ★ 有 input/output tokens + cost
│       model: claude-4.6-opus
│       input_tokens: 176,337
│       output_tokens: 783
│       cost: $0.54
│       cache_hit_rate: 99.5%
│
├── 💭 Thinking                            [Span]        ← ★ 新增：thinking 内容
│       output: "让我分析一下这个问题..."
│
├── 🔧 Tool: Read                          [Tool]
│       input: { file_path: "src/app.ts" }
│       output: "文件内容..."
│
├── 🔧 Tool: Edit                          [Tool]
│       input: { file_path: "src/app.ts", old_string: "...", new_string: "..." }
│       output: "Successfully edited"
│
└── 🔧 Tool: Bash                          [Tool]
        input: { command: "npm test" }
        output: "All tests passed"
```

### 5.2 Dashboard 指标

改造后 Langfuse Dashboard 自动提供：

| 指标 | 说明 |
|------|------|
| **Total Cost** | 按模型定价自动计算（USD），支持按天/周/月汇总 |
| **Token Usage Trend** | input/output tokens 的时间趋势图 |
| **Latency** | 每轮响应耗时 |
| **Model Distribution** | 不同模型（opus/sonnet）的使用占比 |
| **Traces per Session** | 每个会话的 turn 数量 |

### 5.3 metadata 中的额外信息

在每个 Generation 的 metadata 中可以看到：

```json
{
  "input_tokens": 176337,
  "output_tokens": 783,
  "cache_creation_input_tokens": 836,
  "cache_read_input_tokens": 175500,
  "cache_hit_rate_percent": 99.52,
  "stop_reason": "end_turn",
  "assistant_message_count": 3,
  "has_thinking": true,
  "thinking_length": 4521,
  "tool_count": 5
}
```

---

## 六、与 Cursor Hook 方案的对比

| 维度 | Cursor Hooks → Langfuse | Claude Code Hooks → Langfuse（本方案） |
|------|------------------------|---------------------------------------|
| 数据来源 | IDE 事件通知（event-level） | transcript 文件（API-level） |
| input_tokens | ❌ 拿不到 | ✅ 精确到每轮 |
| output_tokens | ❌ 拿不到 | ✅ 精确到每轮 |
| 成本计算 | ❌ | ✅ Langfuse 自动算 |
| cache 命中 | ❌ | ✅ cache_read / cache_creation |
| 工具调用输入 | 部分（仅文件名等） | ✅ 完整 tool_use.input |
| 工具调用输出 | ❌ | ✅ 完整 tool_result |
| thinking 内容 | 有（afterAgentThought.text） | ✅ thinking block 完整内容 |
| stop_reason | ❌ | ✅ end_turn / tool_use |
| 需要改 CLI 源码 | 否 | 否 |
| 原理 | IDE 事件 hook → Langfuse SDK | Stop hook → 读 transcript → Langfuse SDK |

---

## 七、调试与排障

### 7.1 没有 trace 出现

```bash
# 1. 检查 hook 是否在运行
tail -f ~/.claude/state/langfuse_hook.log

# 2. 开启 debug 模式（在 .claude/settings.local.json 中）
"CC_LANGFUSE_DEBUG": "true"

# 3. 确认环境变量
echo $TRACE_TO_LANGFUSE  # 应该是 true

# 4. 确认 langfuse SDK 已安装
pip show langfuse
```

### 7.2 手动测试脚本

```bash
TRACE_TO_LANGFUSE=true \
LANGFUSE_PUBLIC_KEY="pk-lf-..." \
LANGFUSE_SECRET_KEY="sk-lf-..." \
echo '{"sessionId":"test-123","transcriptPath":"~/.claude/projects/.../xxx.jsonl"}' | \
python3 ~/.claude/hooks/langfuse_hook.py
```

### 7.3 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| hook 不触发 | settings.json 格式错误 | 用 `claude /settings` 检查 |
| Permission denied | 脚本没有执行权限 | `chmod +x langfuse_hook.py` |
| Windows fcntl 报错 | fcntl 是 Unix-only | 脚本已做兼容处理，会 fallback 到 msvcrt |
| transcript 路径不存在 | session 还没产生 transcript | 等 Claude Code 产生至少一轮对话 |
| usage 全为 0 | transcript 中该 assistant 消息确实没有 usage | 检查 transcript 原始内容确认 |

---

## 八、相关参考

| 资源 | 链接 |
|------|------|
| Langfuse 官方 Claude Code 集成 | https://langfuse.com/integrations/other/claude-code |
| Claude Code Hooks 文档 | https://code.claude.com/docs/en/hooks-guide |
| Claude Code Monitoring 文档 | https://code.claude.com/docs/en/monitoring-usage |
| Langfuse Python SDK | https://python.reference.langfuse.com/ |
| claude-langfuse-monitor（零配置替代方案） | https://github.com/michaeloboyle/claude-langfuse-monitor |
| ObservAgent（本地仪表盘） | https://github.com/darshannere/observagent |
| claude-code-hooks-multi-agent-observability | https://github.com/disler/claude-code-hooks-multi-agent-observability |
