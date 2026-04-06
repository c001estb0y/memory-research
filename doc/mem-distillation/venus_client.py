"""Venus API 客户端：日志、限流、重试"""

from __future__ import annotations

import logging
import os
import sys
import time as _time
from collections import deque
from pathlib import Path

import requests


# ── 加载 .env 文件 ──

def _load_dotenv(env_path: str = ".env"):
    p = Path(env_path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


# ── 日志 ──

def setup_logger(
    name: str = "distiller",
    log_file: str = "distill.log",
) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()


# ── Venus API 配置 ──

VENUS_URL = os.environ.get(
    "VENUS_URL",
    "http://v2.open.venus.oa.com/llmproxy/chat/completions",
)
VENUS_MODEL = os.environ.get("VENUS_MODEL", "claude-sonnet-4-6")
VENUS_RPM_LIMIT = int(os.environ.get("VENUS_RPM_LIMIT", "50"))


def _get_venus_token() -> str:
    token = os.environ.get("VENUS_TOKEN")
    if token:
        return token
    secret_id = os.environ.get("ENV_VENUS_OPENAPI_SECRET_ID", "")
    return f"{secret_id}@5172"


# ── 滑动窗口限流器 ──

class RateLimiter:
    def __init__(self, max_calls: int = VENUS_RPM_LIMIT, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window = window_seconds
        self.calls: deque = deque()

    def wait(self):
        now = _time.monotonic()
        while self.calls and self.calls[0] <= now - self.window:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            sleep_until = self.calls[0] + self.window
            sleep_time = sleep_until - now
            if sleep_time > 0:
                logger.warning(
                    f"[RateLimit] 达到 {self.max_calls} 次/分钟上限，"
                    f"等待 {sleep_time:.1f}s..."
                )
                _time.sleep(sleep_time)
        self.calls.append(_time.monotonic())


_rate_limiter = RateLimiter(max_calls=VENUS_RPM_LIMIT, window_seconds=60)


# ── 统一 API 调用入口 ──

def call_venus_api(
    payload: dict,
    max_retries: int = 3,
    retry_delay: float = 5.0,
) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_get_venus_token()}",
    }

    model = payload.get("model", "unknown")
    prompt_text = str(payload.get("messages", ""))
    est_input_tokens = len(prompt_text) // 2

    for attempt in range(1, max_retries + 1):
        _rate_limiter.wait()

        logger.debug(
            f"[Venus] 请求 model={model}, est_input≈{est_input_tokens} tokens, "
            f"attempt={attempt}/{max_retries}"
        )

        start = _time.monotonic()
        try:
            response = requests.post(
                VENUS_URL, headers=headers, json=payload, timeout=120
            )
            elapsed_ms = (_time.monotonic() - start) * 1000

            if response.status_code == 429:
                logger.warning(
                    f"[Venus] 429 Too Many Requests, 等待 {retry_delay}s 后重试 "
                    f"(attempt {attempt}/{max_retries})"
                )
                _time.sleep(retry_delay)
                retry_delay *= 2
                continue

            if response.status_code != 200:
                logger.error(
                    f"[Venus] HTTP {response.status_code}, "
                    f"body={response.text[:500]}, elapsed={elapsed_ms:.0f}ms"
                )
                if attempt < max_retries:
                    _time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                raise RuntimeError(
                    f"Venus API error {response.status_code}: {response.text[:200]}"
                )

            result = response.json()
            usage = result.get("usage", {})
            logger.info(
                f"[Venus] 成功 | {elapsed_ms:.0f}ms | "
                f"input={usage.get('prompt_tokens', '?')} "
                f"output={usage.get('completion_tokens', '?')} tokens"
            )
            return result

        except requests.exceptions.RequestException as e:
            elapsed_ms = (_time.monotonic() - start) * 1000
            logger.error(
                f"[Venus] 网络异常: {e}, elapsed={elapsed_ms:.0f}ms, "
                f"attempt={attempt}/{max_retries}"
            )
            if attempt < max_retries:
                _time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise

    raise RuntimeError(f"Venus API 调用失败，已重试 {max_retries} 次")
