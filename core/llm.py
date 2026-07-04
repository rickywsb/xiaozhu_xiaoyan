"""core/llm.py — OpenAI LLM 轻封装（结构化 JSON 输出 + 密钥多级读取 + 成本日志）

密钥读取优先级：
  1) st.secrets["OPENAI_API_KEY"]      —— Streamlit Cloud 部署时用这个
  2) 环境变量 OPENAI_API_KEY
  3) 本地 openaitoken.txt（仅本地开发兜底；已在 .gitignore，不会提交）

所有调用都走服务端，密钥不会下发到前端。异常统一抛 LLMError，页面负责降级提示。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 模型分层：日常用便宜的 mini，深度诊断可切大模型
DEFAULT_MODEL = "gpt-4o-mini"
DEEP_MODEL = "gpt-4.1"

# 粗略计价（USD / 1M tokens），仅用于页面成本提示，非精确账单
_PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1":     (2.00, 8.00),
    "gpt-4o":      (2.50, 10.00),
}


class LLMError(Exception):
    """LLM 调用相关的统一异常。"""


# ─── 密钥 ─────────────────────────────────────────────────────────────────────

def get_api_key() -> str | None:
    # 1) Streamlit secrets
    try:
        import streamlit as st  # noqa: PLC0415
        try:
            if "OPENAI_API_KEY" in st.secrets:
                val = str(st.secrets["OPENAI_API_KEY"]).strip()
                if val:
                    return val
        except Exception:
            pass
    except Exception:
        pass

    # 2) 环境变量
    env = os.environ.get("OPENAI_API_KEY")
    if env and env.strip():
        return env.strip()

    # 3) 本地 txt（开发兜底）
    try:
        import config  # noqa: PLC0415
        p = config.APP_DIR / "openaitoken.txt"
        if p.exists():
            raw = p.read_text(encoding="utf-8").strip()
            if raw:
                first = raw.splitlines()[0].strip()
                # 兼容 "OPENAI_API_KEY=sk-..." 与 "sk-..." 两种写法
                if "=" in first and first.split("=", 1)[0].strip().upper().endswith("KEY"):
                    first = first.split("=", 1)[1].strip()
                return first.strip().strip('"').strip("'")
    except Exception:
        pass

    return None


def available() -> bool:
    """是否已配置密钥（页面据此决定显示按钮还是提示）。"""
    return bool(get_api_key())


# ─── 客户端 ───────────────────────────────────────────────────────────────────

def _client():
    key = get_api_key()
    if not key:
        raise LLMError(
            "未找到 OPENAI_API_KEY。请在 Streamlit Secrets、环境变量，"
            "或本地 openaitoken.txt 中配置后重试。"
        )
    try:
        from openai import OpenAI  # noqa: PLC0415
    except ImportError as exc:
        raise LLMError("未安装 openai 库：请先 `pip install openai`。") from exc
    return OpenAI(api_key=key)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    inp, out = _PRICING.get(model, (0.0, 0.0))
    return prompt_tokens / 1e6 * inp + completion_tokens / 1e6 * out


# ─── 结构化对话 ───────────────────────────────────────────────────────────────

def chat_json(
    system: str,
    user: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.2,
    max_tokens: int = 1400,
) -> dict:
    """调用 Chat Completions，强制 JSON 输出，返回解析后的 dict。

    返回值附带 `_usage`（token 用量）、`_model`、`_cost_usd`（粗估）三个内部字段。
    失败抛 LLMError。
    """
    client = _client()
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as exc:  # 网络 / 鉴权 / 限流等
        raise LLMError(f"OpenAI 调用失败：{exc}") from exc

    content = (resp.choices[0].message.content or "").strip() or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError("模型返回的不是合法 JSON。") from exc
    if not isinstance(data, dict):
        raise LLMError("模型返回的 JSON 顶层不是对象。")

    usage = getattr(resp, "usage", None)
    if usage is not None:
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        data["_usage"] = {"prompt_tokens": pt, "completion_tokens": ct,
                          "total_tokens": getattr(usage, "total_tokens", pt + ct)}
        data["_cost_usd"] = round(estimate_cost(model, pt, ct), 5)
    data["_model"] = model
    return data
