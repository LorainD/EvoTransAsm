from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ComputationIR:
    type: str = "unknown"
    expression_tree: dict[str, Any] = field(default_factory=lambda: {"op": "unknown", "inputs": [], "params": {}})


@dataclass
class MemoryIR:
    access_pattern: str = "contiguous"
    stride: str = "none"
    alignment: str = "unknown"
    layout: str = "1D"


@dataclass
class ParallelismIR:
    vectorizable: bool = False
    reduction: bool = False
    dependency: str = "unknown"
    tail_policy: str = "none"


@dataclass
class IR:
    computation: ComputationIR = field(default_factory=ComputationIR)
    memory: MemoryIR = field(default_factory=MemoryIR)
    parallelism: ParallelismIR = field(default_factory=ParallelismIR)


def default_ir() -> dict[str, Any]:
    return asdict(IR())


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return default


def normalize_ir(value: Any) -> dict[str, Any]:
    """Coerce arbitrary payload into the canonical IR dict shape."""
    base = default_ir()
    if not isinstance(value, dict):
        return base

    comp = value.get("computation", {}) if isinstance(value.get("computation"), dict) else {}
    mem = value.get("memory", {}) if isinstance(value.get("memory"), dict) else {}
    par = value.get("parallelism", {}) if isinstance(value.get("parallelism"), dict) else {}

    expression_tree = comp.get("expression_tree", {})
    if not isinstance(expression_tree, dict):
        expression_tree = {"op": "unknown", "inputs": [], "params": {}}
    expression_tree.setdefault("op", "unknown")
    if not isinstance(expression_tree.get("inputs"), list):
        expression_tree["inputs"] = []
    if not isinstance(expression_tree.get("params"), dict):
        expression_tree["params"] = {}

    base["computation"] = {
        "type": str(comp.get("type", "unknown") or "unknown"),
        "expression_tree": expression_tree,
    }
    base["memory"] = {
        "access_pattern": str(mem.get("access_pattern", "contiguous") or "contiguous"),
        "stride": str(mem.get("stride", "none") or "none"),
        "alignment": str(mem.get("alignment", "unknown") or "unknown"),
        "layout": str(mem.get("layout", "1D") or "1D"),
    }
    base["parallelism"] = {
        "vectorizable": _normalize_bool(par.get("vectorizable", False), default=False),
        "reduction": _normalize_bool(par.get("reduction", False), default=False),
        "dependency": str(par.get("dependency", "unknown") or "unknown"),
        "tail_policy": str(par.get("tail_policy", "none") or "none"),
    }
    return base


def extract_ir_tags(ir: dict[str, Any]) -> list[str]:
    """Build compact IR tags for lightweight search/filtering."""
    normalized = normalize_ir(ir)
    comp = normalized["computation"]
    mem = normalized["memory"]
    par = normalized["parallelism"]
    tags = [
        f"comp:{comp.get('type', 'unknown')}",
        f"mem:{mem.get('access_pattern', 'contiguous')}",
        f"stride:{mem.get('stride', 'none')}",
        f"dep:{par.get('dependency', 'unknown')}",
        f"tail:{par.get('tail_policy', 'none')}",
    ]
    if par.get("vectorizable", False):
        tags.append("vec:true")
    if par.get("reduction", False):
        tags.append("red:true")
    return tags


def ir_match_score(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, list[str]]:
    """Return [0, 1] score and reasons for IR similarity."""
    l = normalize_ir(left)
    r = normalize_ir(right)
    score = 0.0
    reasons: list[str] = []

    if l["computation"]["type"] == r["computation"]["type"]:
        score += 0.35
        reasons.append("computation.type")

    if l["memory"]["access_pattern"] == r["memory"]["access_pattern"]:
        score += 0.2
        reasons.append("memory.access_pattern")
    if l["memory"]["stride"] == r["memory"]["stride"]:
        score += 0.1
        reasons.append("memory.stride")

    if l["parallelism"]["vectorizable"] == r["parallelism"]["vectorizable"]:
        score += 0.15
        reasons.append("parallelism.vectorizable")
    if l["parallelism"]["reduction"] == r["parallelism"]["reduction"]:
        score += 0.1
        reasons.append("parallelism.reduction")
    if l["parallelism"]["tail_policy"] == r["parallelism"]["tail_policy"]:
        score += 0.1
        reasons.append("parallelism.tail_policy")

    return max(0.0, min(1.0, score)), reasons
