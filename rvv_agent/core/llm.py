from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import LlmConfig
from .util import extract_json_from_llm

#TODO:按照之前patch、search部分，应该要添加llm的错误处理，特别是断线重连部分
@dataclass(frozen=True)
class LlmMessage:
    role: str  # system | user | assistant
    content: str


class LlmError(RuntimeError):
    """Base class for LLM call failures."""
    def __init__(self, message: str, *, retryable: bool = False, status_code: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Per-session trajectory (reset before each pipeline run, saved by caller)
# ---------------------------------------------------------------------------

@dataclass
class TrajectoryEvent:
    timestamp: str
    stage: str
    prompt: str           # last user message
    response: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    cumulative_input_tokens: int
    cumulative_output_tokens: int
    cumulative_cost_usd: float
    elapsed_seconds: float

    def to_dict(self) -> dict:
        return {
            "event_type": "llm_call",
            "timestamp": self.timestamp,
            "stage": self.stage,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "cumulative_input_tokens": self.cumulative_input_tokens,
            "cumulative_output_tokens": self.cumulative_output_tokens,
            "cumulative_cost_usd": round(self.cumulative_cost_usd, 8),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "prompt": self.prompt[:2000].splitlines(),
            "response": self.response[:4000].splitlines(),
        }


@dataclass
class ActionEvent:
    """Non-LLM trajectory event: agent action or human-facing output."""
    timestamp: str
    event_type: str   # "action" | "human_output"
    stage: str
    description: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "stage": self.stage,
            "description": self.description,
            "detail": self.detail[:4000],
        }


class _Trajectory:
    def __init__(self) -> None:
        self._events: list = []  # TrajectoryEvent | ActionEvent
        self._cum_in = 0
        self._cum_out = 0
        self._cum_cost = 0.0

    def reset(self) -> None:
        self._events = []
        self._cum_in = 0
        self._cum_out = 0
        self._cum_cost = 0.0

    def record(
        self,
        stage: str,
        prompt: str,
        response: str,
        input_tokens: int,
        output_tokens: int,
        cost_per_1m_in: float,
        cost_per_1m_out: float,
        elapsed: float,
    ) -> None:
        cost = (input_tokens * cost_per_1m_in + output_tokens * cost_per_1m_out) / 1_000_000
        self._cum_in += input_tokens
        self._cum_out += output_tokens
        self._cum_cost += cost
        evt = TrajectoryEvent(
            timestamp=dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            stage=stage,
            prompt=prompt,
            response=response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=cost,
            cumulative_input_tokens=self._cum_in,
            cumulative_output_tokens=self._cum_out,
            cumulative_cost_usd=self._cum_cost,
            elapsed_seconds=elapsed,
        )
        self._events.append(evt)

    def record_action(
        self,
        stage: str,
        description: str,
        detail: str = "",
        event_type: str = "action",
    ) -> None:
        """Record a non-LLM agent action or human-facing output event."""
        evt = ActionEvent(
            timestamp=dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
            event_type=event_type,
            stage=stage,
            description=description,
            detail=detail,
        )
        self._events.append(evt)

    def to_dict(self, model: str = "", endpoint: str = "") -> dict:
        llm_calls = [e for e in self._events if isinstance(e, TrajectoryEvent)]
        return {
            "model": model,
            "endpoint": endpoint,
            "events": [e.to_dict() for e in self._events],
            "totals": {
                "input_tokens": self._cum_in,
                "output_tokens": self._cum_out,
                "total_tokens": self._cum_in + self._cum_out,
                "cost_usd": round(self._cum_cost, 8),
                "num_calls": len(llm_calls),
                "num_events": len(self._events),
            },
        }

    def __len__(self) -> int:
        return len(self._events)


# Module-level singleton – one per process
_TRAJECTORY = _Trajectory()


def reset_trajectory() -> None:
    """Reset trajectory at the start of a new pipeline run."""
    _TRAJECTORY.reset()


def get_trajectory_dict(model: str = "", endpoint: str = "") -> dict:
    return _TRAJECTORY.to_dict(model=model, endpoint=endpoint)


def record_trajectory_action(
    stage: str,
    description: str,
    detail: str = "",
    event_type: str = "action",
) -> None:
    """Record a non-LLM agent action or human-facing output to the trajectory."""
    _TRAJECTORY.record_action(stage, description, detail, event_type)


# ---------------------------------------------------------------------------
# Pricing helpers – sensible defaults for common models
# ---------------------------------------------------------------------------

_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # model-name-prefix → ($/1M input, $/1M output)
    "gpt-5.4":(2.5,15),
    "claude-sonnet-4-6":(0.9,4.5),
    "claude-opus-4-6":(1.5,7.5),
    "gemini-3-pro-preview":(1.0,6.0),
    "gemini-3.1-pro-preview":(1.0,6.0),
}


def _pricing_for_model(cfg: LlmConfig) -> tuple[float, float]:
    # Allow override in config
    if hasattr(cfg, "cost_per_1m_input_tokens") and cfg.cost_per_1m_input_tokens > 0:
        return cfg.cost_per_1m_input_tokens, cfg.cost_per_1m_output_tokens
    m = cfg.model.lower()
    for prefix, price in _DEFAULT_PRICING.items():
        if m.startswith(prefix):
            return price
    return 0.0, 0.0  # unknown model – no cost estimate


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def api_key_present(cfg: LlmConfig) -> bool:
    return bool(os.getenv(cfg.api_key_env, "").strip())


def llm_status(cfg: LlmConfig) -> dict[str, object]:
    return {
        "endpoint_url": cfg.base_url,
        "model": cfg.model,
        "api_key_env": cfg.api_key_env,
        "api_key_present": api_key_present(cfg),
        "temperature": cfg.temperature,
    }


def _endpoint_url(cfg: LlmConfig) -> str:
    url = (cfg.base_url or "").strip().rstrip("/")
    if not url:
        raise LlmError("Missing LLM endpoint URL: set llm.base_url in rvv_agent.toml")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    return url


def _embedding_endpoint_url(cfg: LlmConfig) -> str:
    url = (cfg.base_url or "").strip().rstrip("/")
    if not url:
        raise LlmError("Missing LLM endpoint URL: set llm.base_url in rvv_agent.toml")
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/embeddings"
    if not url.endswith("/embeddings"):
        url = url + "/embeddings"
    return url


def get_text_embedding(text: str, cfg: LlmConfig, *, timeout_seconds: float = 60.0) -> list[float]:
    """Get embedding vector via OpenAI-compatible /embeddings endpoint.

    Returns empty list on failure to keep callers resilient.
    """
    text_in = str(text or "").strip()
    if not text_in:
        return []

    api_key = os.getenv(cfg.api_key_env, "").strip()
    if not api_key:
        return []

    model = str(getattr(cfg, "model", "") or "").strip()
    if not model:
        return []
    url = _embedding_endpoint_url(cfg)
    payload = {
        "model": model,
        "input": text_in[:3000],
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=_headers(api_key),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        emb = data.get("data", [{}])[0].get("embedding", [])
        if isinstance(emb, list):
            out: list[float] = []
            for x in emb:
                try:
                    out.append(float(x))
                except Exception:
                    return []
            return out
        return []
    except Exception:
        return []


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity without third-party dependencies."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = 0.0
    n1 = 0.0
    n2 = 0.0
    for a, b in zip(v1, v2):
        dot += a * b
        n1 += a * a
        n2 += b * b
    if n1 <= 0.0 or n2 <= 0.0:
        return 0.0
    return float(dot / (math.sqrt(n1) * math.sqrt(n2)))


def _is_retryable_http(status_code: int) -> bool:
    """Check if an HTTP status code is retryable."""
    return status_code in (429, 500, 502, 503, 504)


def _is_retryable_error(err: Exception) -> bool:
    """Check if an exception is retryable (network/timeout errors)."""
    msg = str(err).lower()
    if isinstance(err, LlmError):
        return err.retryable
    if isinstance(err, urllib.error.HTTPError):
        return _is_retryable_http(err.code)
    # Network / timeout errors
    return any(kw in msg for kw in ("timeout", "timed out", "urlopen error",
                                     "connection", "temporary failure",
                                     "name resolution"))


def chat_completion(
    cfg: LlmConfig,
    messages: list[LlmMessage],
    *,
    max_tokens: int = 2048,
    timeout_seconds: float = 120.0,
    stage: str = "llm",
    max_retries: int = 2,
    retry_delay: float = 3.0,
) -> str:
    """Call the LLM and return the assistant text, with automatic retry.

    Retryable errors (429, 5xx, timeout, network) are retried up to
    *max_retries* times with linear backoff: delay = retry_delay * (attempt+1).
    Non-retryable errors (401, 400) are raised immediately.

    Also appends a :class:`TrajectoryEvent` to the module-level trajectory so
    callers can save it to ``trajectory.json`` with :func:`get_trajectory_dict`.
    """
    api_key = os.getenv(cfg.api_key_env, "").strip()
    if not api_key:
        raise LlmError(f"Missing API key: env {cfg.api_key_env} is empty")

    url = _endpoint_url(cfg)

    payload: dict[str, Any] = {
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }

    # Build prompt string for trajectory (last user message)
    prompt_text = ""
    for m in reversed(messages):
        if m.role == "user":
            prompt_text = m.content
            break

    price_in, price_out = _pricing_for_model(cfg)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            wait = retry_delay * attempt
            print(f"[LLM] 重试 {attempt}/{max_retries}（等待 {wait:.0f}s）…")
            time.sleep(wait)

        t0 = time.monotonic()
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=_headers(api_key),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            elapsed = time.monotonic() - t0
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            err_msg = f"HTTP {e.code} from {url}: {body[:2000]}"
            _TRAJECTORY.record(
                stage=stage + "_error",
                prompt=prompt_text,
                response=f"ERROR: {err_msg}",
                input_tokens=0, output_tokens=0,
                cost_per_1m_in=price_in, cost_per_1m_out=price_out,
                elapsed=elapsed,
            )
            last_error = LlmError(err_msg, retryable=_is_retryable_http(e.code),
                                   status_code=e.code)
            if not _is_retryable_http(e.code) or attempt >= max_retries:
                raise last_error from e
            continue
        except Exception as e:
            elapsed = time.monotonic() - t0
            err_msg = f"LLM request failed at {url}: {e}"
            _TRAJECTORY.record(
                stage=stage + "_error",
                prompt=prompt_text,
                response=f"ERROR: {err_msg}",
                input_tokens=0, output_tokens=0,
                cost_per_1m_in=price_in, cost_per_1m_out=price_out,
                elapsed=elapsed,
            )
            last_error = LlmError(err_msg, retryable=_is_retryable_error(e))
            if not _is_retryable_error(e) or attempt >= max_retries:
                raise last_error from e
            continue

        elapsed = time.monotonic() - t0

        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except Exception as e:
            err_msg = f"Unexpected LLM response: {raw[:2000]}"
            _TRAJECTORY.record(
                stage=stage + "_parse_error",
                prompt=prompt_text,
                response=f"ERROR: {err_msg}",
                input_tokens=0, output_tokens=0,
                cost_per_1m_in=price_in, cost_per_1m_out=price_out,
                elapsed=elapsed,
            )
            raise LlmError(err_msg) from e

        # Parse usage from response
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))

        _TRAJECTORY.record(
            stage=stage,
            prompt=prompt_text,
            response=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_per_1m_in=price_in,
            cost_per_1m_out=price_out,
            elapsed=elapsed,
        )

        return content

    # Should not reach here, but just in case
    raise last_error or LlmError("LLM call failed after retries")


def _is_retryable_llm_error(error: LlmError) -> bool:
    """Best-effort check for whether an LlmError should be retried."""
    if getattr(error, "retryable", False):
        return True
    msg = str(error).lower()
    retryable_keywords = (
        "timeout",
        "timed out",
        "connection",
        "network",
        "temporarily unavailable",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
    )
    return any(k in msg for k in retryable_keywords)


def _resolve_stage_max_tokens(cfg: LlmConfig, stage: str, requested: int) -> int:
    """Resolve max_tokens with global defaults and optional per-stage overrides.

    Rule:
    - Start from cfg.default_max_tokens.
    - If cfg.stage_max_tokens has current stage, use that override.
    - Final max_tokens is max(requested, configured) so legacy small constants
      do not cap new global settings.
    """
    configured = int(getattr(cfg, "default_max_tokens", 2048) or 2048)
    stage_cfg = getattr(cfg, "stage_max_tokens", {})
    if isinstance(stage_cfg, dict):
        try:
            ov = int(stage_cfg.get(stage, configured))
            if ov > 0:
                configured = ov
        except Exception:
            pass

    resolved = max(int(requested or 0), configured)
    if resolved <= 0:
        return 2048
    return resolved


def chat_completion_with_retry(
    cfg: LlmConfig,
    messages: list[LlmMessage],
    *,
    max_tokens: int = 2048,
    timeout_seconds: float = 120.0,
    stage: str = "llm",
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> str:
    """Call LLM with stage-level retry policy and exponential backoff.

    This wrapper centralizes retry behavior for call sites.
    """
    last_error: LlmError | None = None
    attempts = max(1, max_retries)
    resolved_max_tokens = _resolve_stage_max_tokens(cfg, stage, max_tokens)
    for attempt in range(attempts):
        try:
            # Disable internal retry to avoid double-retry loops.
            return chat_completion(
                cfg,
                messages,
                max_tokens=resolved_max_tokens,
                timeout_seconds=timeout_seconds,
                stage=stage,
                max_retries=0,
            )
        except LlmError as e:
            last_error = e
            can_retry = _is_retryable_llm_error(e)
            if not can_retry or attempt >= attempts - 1:
                raise
            wait_time = retry_delay * (2 ** attempt)
            print(f"[LLM] {stage} 失败，{wait_time:.1f}s 后重试 ({attempt + 1}/{attempts}): {str(e)[:120]}")
            time.sleep(wait_time)

    raise last_error or LlmError(f"LLM call failed after {attempts} attempts")


def probe_llm(cfg: LlmConfig) -> dict[str, object]:
    """Probe LLM endpoint health without raising on failure.

    返回一个 status dict，至少包含：
    - endpoint_url / model / api_key_present
    - probe_ok: bool
    - probe_reply 或 probe_error
    """

    status = llm_status(cfg)
    try:
        status["endpoint_url_normalized"] = _endpoint_url(cfg)
    except Exception as e:  # e.g. missing base_url
        status["probe_ok"] = False
        status["probe_error"] = str(e)
        return status

    probe_timeout = float(os.getenv("RVV_AGENT_LLM_PROBE_TIMEOUT", "10"))

    try:
        text = chat_completion(
            cfg,
            [
                LlmMessage(role="system", content="You are a helpful assistant."),
                LlmMessage(role="user", content="Reply with: OK"),
            ],
            max_tokens=8,
            timeout_seconds=probe_timeout,
            stage="probe",
        )
        status["probe_ok"] = True
        status["probe_reply"] = text.strip()[:200]
        return status
    except Exception as e:  # noqa: BLE001
        status["probe_ok"] = False
        status["probe_error"] = str(e)
        return status


# ---------------------------------------------------------------------------
# Generic tool-use loop (ReAct-style over plain chat completions)
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """Lightweight description of a callable tool for tool-use loops.

    ``func`` 接收解析后的 ``arguments`` 字典，返回任意可 JSON 序列化的对象。

    这里不直接绑定到底层 LLM 的原生 function-calling 协议，而是约定：
    - LLM 通过普通文本输出一个 JSON，对应一次 tool 调用或最终结果；
    - JSON 结构由上层 prompt 约定（见 ``_parse_tool_message`` 的约定）。
    这样可以在任意兼容 /chat/completions 的服务上工作。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[[dict[str, Any]], Any]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


def _parse_tool_message(raw: str) -> tuple[str, ToolCall | None, Any | None]:
    """Parse a model reply into either a tool call or a final result.

    约定的 JSON 结构（由上层 prompt 约束 LLM 输出）：

    - 请求调用工具：
      {"tool_call": {"name": "search_files", "arguments": {"pattern": "..."}}}

    - 返回最终结果：
      {"final": { ... 任意结果 ... }}

    如果解析失败或没有匹配字段，则退化为 "final"，并把整体 JSON
    或原始文本作为结果交给上层，由上层自行解释。
    """

    raw = (raw or "").strip()
    if not raw:
        return "final", None, None

    try:
        data = extract_json_from_llm(raw)
    except Exception:
        # 无法解析为 JSON 时，视为最终自然语言结果
        return "final", None, raw

    if not isinstance(data, dict):
        return "final", None, data

    tc = data.get("tool_call")
    if isinstance(tc, dict):
        name = str(tc.get("name", "")).strip()
        args = tc.get("arguments") or tc.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if name:
            return "tool", ToolCall(name=name, arguments=args), None

    # 显式 final 包装
    if "final" in data:
        return "final", None, data.get("final")

    # 回退：把整个对象视为最终结果
    return "final", None, data


def run_tool_use_loop(
    cfg: LlmConfig,
    messages: list[LlmMessage],
    tools: list[ToolSpec],
    *,
    max_rounds: int = 8,
    max_tokens: int = 2048,
    timeout_seconds: float = 120.0,
    stage: str = "tools",
) -> tuple[list[LlmMessage], Any | None]:
    """Run a simple ReAct-style loop where the LLM can request tools.

    - ``messages``: 现有对话历史（必须包含 system / user 上下文）。
    - ``tools``: 可用工具列表，每个带有 name/description/parameters/func。
    - LLM 每轮通过 JSON 描述要调用的工具或给出最终结果。

    返回值为 (最终 messages, 最终结果)。最终结果通常是 JSON 对象，
    但也可以是自然语言字符串（在无法解析 JSON 时）。
    """

    tool_map = {t.name: t for t in tools}
    final_result: Any | None = None

    for _ in range(max_rounds):
        reply = chat_completion_with_retry(
            cfg,
            messages,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            stage=stage,
        )
        messages.append(LlmMessage(role="assistant", content=reply))

        kind, call, result = _parse_tool_message(reply)
        if kind == "tool" and call is not None:
            spec = tool_map.get(call.name)
            if spec is None:
                err_msg = f"Unknown tool: {call.name}"
                record_trajectory_action(stage, f"tool_error: {err_msg}", event_type="action")
                # 反馈给模型，让其自行修正工具名或参数
                messages.append(
                    LlmMessage(
                        role="user",
                        content=f"TOOL_ERROR: {err_msg}",
                    )
                )
                continue

            try:
                tool_output = spec.func(call.arguments or {})
                payload = json.dumps(
                    {"tool_name": spec.name, "ok": True, "result": tool_output},
                    ensure_ascii=False,
                )
            except Exception as e:  # noqa: BLE001
                err_text = f"{type(e).__name__}: {e}"
                payload = json.dumps(
                    {"tool_name": spec.name, "ok": False, "error": err_text},
                    ensure_ascii=False,
                )

            # 将工具执行结果作为新的 user 消息反馈给 LLM
            messages.append(
                LlmMessage(
                    role="user",
                    content=f"TOOL_RESULT: {payload}",
                )
            )
            continue

        # 没有 tool 调用 → 视为最终结果
        if isinstance(result, dict):
            final_result = result
        elif result is not None:
            final_result = result
        else:
            final_result = reply
        break

    return messages, final_result
