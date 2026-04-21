"""memory.knowledge_base — IR-first knowledge base for RVV migration.

Stores two kinds of records:
    - Pattern: reusable migration pattern in canonical IR form.
    - ErrorRecord: recurring build/test errors and their proven fixes.

Storage: a single JSON file (``knowledge_base.json`` by default).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.ir import extract_ir_tags, ir_match_score, normalize_ir


@dataclass
class Pattern:
    """A reusable RVV migration pattern in canonical IR schema."""
    pattern_id: str = ""
    source: dict = field(default_factory=dict)
    ir: dict = field(default_factory=dict)
    simd_features: dict = field(default_factory=dict)
    references: dict = field(default_factory=dict)
    meta: dict = field(default_factory=lambda: {
        "weight": 0.5,
        "stats": {
            "success_count": 0,
            "fail_count": 0,
        },
        "ir_tags": [],
    })
    notes: str = ""


@dataclass
class ErrorRecord:
    """A recurring error pattern and its fix strategy."""
    error_class: str = ""       # compile_error | link_error | runtime_error | test_mismatch
    pattern: str = ""           # description of the error pattern
    fix_strategy: str = ""      # proven fix approach
    example: str = ""           # concrete example (error text snippet)
    count: int = 1
    embedding: list[float] = field(default_factory=list)


class KnowledgeBase:
    """JSON-backed knowledge base with basic CRUD and keyword search."""

    def __init__(self, path: Path | str = "knowledge_base.json") -> None:
        self.path = Path(path)
        self.patterns: list[Pattern] = []
        self.errors: list[ErrorRecord] = []

    # ── persistence ──────────────────────────────────────────────────────

    def load(self) -> None:
        """Load from JSON file.  No-op if file does not exist."""
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.patterns = [self._normalize_pattern_record(p) for p in data.get("patterns", [])]
            self.errors = [self._normalize_error_record(e) for e in data.get("errors", [])]
        except Exception:
            pass  # corrupted file — start fresh

    def save(self) -> None:
        """Persist to JSON file."""
        data = {
            "patterns": [asdict(p) for p in self.patterns],
            "errors": [asdict(e) for e in self.errors],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── pattern CRUD ─────────────────────────────────────────────────────

    def add_pattern(self, p: Pattern) -> None:
        # Deduplicate by pattern_id
        p.ir = normalize_ir(p.ir)
        p.meta = p.meta if isinstance(p.meta, dict) else {}
        p.meta.setdefault("weight", 0.5)
        p.meta.setdefault("stats", {"success_count": 0, "fail_count": 0})
        p.meta["ir_tags"] = extract_ir_tags(p.ir)

        self.patterns = [x for x in self.patterns if x.pattern_id != p.pattern_id]
        self.patterns.append(p)

    def search_patterns(
        self,
        *,
        symbol: str | None = None,
        ir: dict | None = None,
        tags: list[str] | None = None,
        max_results: int = 10,
    ) -> list[Pattern]:
        """Filter patterns by symbol/tags and optional IR similarity."""
        candidates: list[Pattern] = []
        for p in self.patterns:
            if symbol and symbol not in str(p.source.get("symbol", "")):
                continue
            if tags:
                p_tags = set(str(x) for x in p.meta.get("ir_tags", []))
                if not p_tags.intersection(str(t) for t in tags):
                    continue
            candidates.append(p)

        if ir:
            ranked = self.match_patterns_by_ir(ir, max_results=max_results, candidates=candidates)
            return [x["pattern"] for x in ranked]

        candidates.sort(key=lambda x: float(x.meta.get("weight", 0.0)), reverse=True)
        return candidates[:max_results]

    def match_patterns_by_ir(
        self,
        ir: dict,
        *,
        max_results: int = 3,
        candidates: list[Pattern] | None = None,
    ) -> list[dict]:
        """Return top matched patterns by IR similarity."""
        source = candidates if candidates is not None else self.patterns
        ranked: list[dict] = []
        for p in source:
            score, reasons = ir_match_score(ir, p.ir)
            if score <= 0:
                continue
            ranked.append(
                {
                    "pattern": p,
                    "pattern_id": p.pattern_id,
                    "score": score,
                    "reason": ", ".join(reasons),
                }
            )
        ranked.sort(
            key=lambda x: (
                float(x.get("score", 0.0)),
                float(getattr(x.get("pattern"), "meta", {}).get("weight", 0.0)),
            ),
            reverse=True,
        )
        return ranked[:max_results]

    def update_weight(self, pattern_id: str, success: bool) -> None:
        """Adjust pattern weight: success → +1, failure → -0.5."""
        for p in self.patterns:
            if p.pattern_id == pattern_id:
                meta = p.meta if isinstance(p.meta, dict) else {}
                stats = meta.get("stats", {}) if isinstance(meta.get("stats", {}), dict) else {}
                if success:
                    stats["success_count"] = int(stats.get("success_count", 0)) + 1
                    meta["weight"] = float(meta.get("weight", 0.5)) + 1.0
                else:
                    stats["fail_count"] = int(stats.get("fail_count", 0)) + 1
                    meta["weight"] = max(0.0, float(meta.get("weight", 0.5)) - 0.5)
                meta["stats"] = stats
                p.meta = meta
                break

    # ── error CRUD ───────────────────────────────────────────────────────

    def add_error(self, e: ErrorRecord, cfg=None) -> None:
        if not e.embedding and cfg is not None and e.pattern:
            try:
                from ..core.llm import get_text_embedding

                feature_text = f"Error: {e.pattern}\nFix: {e.fix_strategy}"
                e.embedding = get_text_embedding(feature_text, cfg.llm if hasattr(cfg, "llm") else cfg)
            except Exception:
                pass

        # Merge with existing record if same class + pattern
        for existing in self.errors:
            if existing.error_class == e.error_class and existing.pattern == e.pattern:
                existing.count += 1
                if e.fix_strategy:
                    existing.fix_strategy = e.fix_strategy
                if e.embedding:
                    existing.embedding = e.embedding
                return
        self.errors.append(e)

    def search_errors(
        self,
        *,
        error_class: str | None = None,
        keyword: str | None = None,
        max_results: int = 10,
    ) -> list[ErrorRecord]:
        results: list[ErrorRecord] = []
        for e in self.errors:
            if error_class and e.error_class != error_class:
                continue
            if keyword and keyword.lower() not in (e.pattern + e.fix_strategy).lower():
                continue
            results.append(e)
            if len(results) >= max_results:
                break
        results.sort(key=lambda x: x.count, reverse=True)
        return results

    def search_errors_semantic(
        self,
        current_error_log: str,
        cfg,
        *,
        error_class: str | None = None,
        max_results: int = 3,
        min_score: float = 0.6,
    ) -> list[ErrorRecord]:
        query = str(current_error_log or "").strip()
        if not query:
            return []

        try:
            from ..core.llm import cosine_similarity, get_text_embedding

            q_emb = get_text_embedding(query[:500], cfg.llm if hasattr(cfg, "llm") else cfg)
        except Exception:
            q_emb = []

        if not q_emb:
            return self.search_errors(error_class=error_class, keyword=query[:120], max_results=max_results)

        scored: list[tuple[float, ErrorRecord]] = []
        for e in self.errors:
            if error_class and e.error_class != error_class:
                continue
            score = cosine_similarity(q_emb, e.embedding) if e.embedding else 0.0
            if score >= min_score:
                scored.append((score, e))

        if not scored:
            return self.search_errors(error_class=error_class, keyword=query[:120], max_results=max_results)

        scored.sort(key=lambda item: item[0], reverse=True)
        return [rec for _, rec in scored[:max_results]]

    @staticmethod
    def _normalize_pattern_record(raw: dict) -> Pattern:
        if not isinstance(raw, dict):
            return Pattern()

        pattern_id = str(raw.get("pattern_id", ""))
        source = raw.get("source", {}) if isinstance(raw.get("source"), dict) else {}

        ir = raw.get("ir")
        if not isinstance(ir, dict):
            # migrate legacy semantic_ir/simd_strategy fields into canonical IR
            legacy_semantic = raw.get("semantic_ir", {}) if isinstance(raw.get("semantic_ir"), dict) else {}
            legacy_strategy = raw.get("simd_strategy", {}) if isinstance(raw.get("simd_strategy"), dict) else {}
            ir = {
                "computation": {
                    "type": str(legacy_semantic.get("algorithm_class", "unknown") or "unknown"),
                    "expression_tree": {"op": "unknown", "inputs": [], "params": {}},
                },
                "memory": {
                    "access_pattern": str(legacy_semantic.get("memory_pattern", "contiguous") or "contiguous"),
                    "stride": "fixed" if "stride" in str(legacy_semantic.get("memory_pattern", "")).lower() else "none",
                    "alignment": "unknown",
                    "layout": "1D",
                },
                "parallelism": {
                    "vectorizable": bool(legacy_strategy.get("vectorize", False)),
                    "reduction": bool(legacy_strategy.get("reduction", False)),
                    "dependency": "unknown",
                    "tail_policy": "required" if str(legacy_strategy.get("tail_handling", "none")) != "none" else "none",
                },
            }
        ir = normalize_ir(ir)

        simd_features = raw.get("simd_features", {}) if isinstance(raw.get("simd_features"), dict) else {}
        references = raw.get("references", {}) if isinstance(raw.get("references"), dict) else {}
        if not references and isinstance(raw.get("architecture"), dict):
            legacy_arch = raw.get("architecture", {})
            references = {
                "x86": legacy_arch.get("x86", []),
                "arm": legacy_arch.get("neon", []) or legacy_arch.get("arm", []),
                "riscv": legacy_arch.get("rvv", []),
            }

        meta = raw.get("meta", {}) if isinstance(raw.get("meta"), dict) else {}
        if not meta and isinstance(raw.get("metadata"), dict):
            old_meta = raw.get("metadata", {})
            meta = {
                "weight": float(old_meta.get("weight", 0.5)),
                "stats": {
                    "success_count": int(old_meta.get("success_count", 0)),
                    "fail_count": int(old_meta.get("fail_count", 0)),
                },
            }
        meta.setdefault("weight", 0.5)
        meta.setdefault("stats", {"success_count": 0, "fail_count": 0})
        meta["ir_tags"] = extract_ir_tags(ir)

        return Pattern(
            pattern_id=pattern_id,
            source=source,
            ir=ir,
            simd_features=simd_features,
            references=references,
            meta=meta,
            notes=str(raw.get("notes", "")),
        )

    @staticmethod
    def _normalize_error_record(raw: dict) -> ErrorRecord:
        if not isinstance(raw, dict):
            return ErrorRecord()
        emb_raw = raw.get("embedding", [])
        emb: list[float] = []
        if isinstance(emb_raw, list):
            for x in emb_raw:
                try:
                    emb.append(float(x))
                except Exception:
                    emb = []
                    break
        return ErrorRecord(
            error_class=str(raw.get("error_class", "")),
            pattern=str(raw.get("pattern", "")),
            fix_strategy=str(raw.get("fix_strategy", "")),
            example=str(raw.get("example", "")),
            count=int(raw.get("count", 1) or 1),
            embedding=emb,
        )
