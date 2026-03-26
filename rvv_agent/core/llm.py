from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import LlmConfig

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
    "gpt-5.4":(0.3750,2.25),
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


def probe_llm(cfg: LlmConfig) -> dict[str, object]:
    status = llm_status(cfg)
    try:
        status["endpoint_url_normalized"] = _endpoint_url(cfg)
    except Exception as e:
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
    except Exception as e:
        status["probe_ok"] = False
        status["probe_error"] = str(e)
        return status
