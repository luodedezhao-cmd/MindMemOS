"""Shared contracts; this package is copied into each task container."""

from dataclasses import asdict, dataclass, fields
import math
from typing import Any


@dataclass(frozen=True)
class CompressionConfig:
    trigger_tokens: int = 8192
    keep_recent_groups: int = 1
    method: str = "ours"
    timeout_seconds: float = 120.0
    retry_after_steps: int = 3
    dynamic_thresholds: bool = False
    full_trigger_tokens: int | None = None
    execution_mode: str = "sync"
    compressor_model: str | None = None
    compressor_reasoning: dict | None = None

    def __post_init__(self):
        if self.compressor_model is not None and (
                not isinstance(self.compressor_model, str) or not self.compressor_model.strip()
                or any(c.isspace() for c in self.compressor_model)):
            raise ValueError("compressor_model must be a nonempty API model ID without whitespace")
        if self.compressor_reasoning is not None:
            # Forwarded verbatim to the upstream `reasoning` field. Providers differ:
            # some honour effort tiers, some only accept enabled=false. Validate shape
            # only; an unsupported value must surface as an upstream error, not silence.
            if not isinstance(self.compressor_reasoning, dict) or not self.compressor_reasoning:
                raise ValueError("compressor_reasoning must be a nonempty object")
            if set(self.compressor_reasoning) - {"effort", "enabled", "max_tokens", "exclude"}:
                raise ValueError("compressor_reasoning keys must be effort/enabled/max_tokens/exclude")
            eff = self.compressor_reasoning.get("effort")
            if eff is not None and eff not in ("minimal", "low", "medium", "high", "xhigh", "max"):
                raise ValueError("compressor_reasoning.effort must be minimal/low/medium/high/xhigh/max")
        if type(self.dynamic_thresholds) is not bool:
            raise ValueError("dynamic_thresholds must be Boolean")
        if self.dynamic_thresholds and self.method not in ("dual_a",):
            raise ValueError("dynamic_thresholds currently requires method dual_a")
        if self.execution_mode not in ("sync", "async"):
            raise ValueError("execution_mode must be sync or async")
        if self.execution_mode == "async" and self.method not in ("v4", "dual_a"):
            raise ValueError("async execution requires method v4 or dual_a")
        if self.method not in ("ours", "p0", "p1", "dual_threshold", "dual_p1", "dual_mini", "dual_neutral", "dual_a", "v3", "v4"):
            raise ValueError("method must be ours, p0, p1, dual_threshold, dual_p1, dual_mini, dual_neutral, dual_a, v3, or v4")
        for key in ("trigger_tokens", "keep_recent_groups", "retry_after_steps"):
            value = getattr(self, key)
            if type(value) is not int or value < 1:
                raise ValueError(f"{key} must be a positive integer")
        if self.method in ("dual_threshold", "dual_p1", "dual_mini", "dual_neutral", "dual_a", "v3", "v4"):
            if type(self.full_trigger_tokens) is not int or self.full_trigger_tokens <= self.trigger_tokens:
                raise ValueError("full_trigger_tokens must be an integer greater than trigger_tokens")
        elif self.full_trigger_tokens is not None:
            raise ValueError("full_trigger_tokens requires dual_threshold, dual_p1, dual_mini, v3, or v4")
        try:
            valid_timeout = (not isinstance(self.timeout_seconds, bool)
                             and isinstance(self.timeout_seconds, (int, float))
                             and math.isfinite(self.timeout_seconds) and self.timeout_seconds > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("timeout_seconds must be finite and positive")

    @classmethod
    def from_dict(cls, data: dict):
        if not isinstance(data, dict):
            raise ValueError("compression config must be a JSON object")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"unknown compression options: {sorted(unknown)}")
        return cls(**data)

    def to_dict(self):
        result = asdict(self)
        if self.full_trigger_tokens is None:
            result.pop("full_trigger_tokens")
        if self.execution_mode == "sync":
            result.pop("execution_mode")
        if not self.dynamic_thresholds:
            result.pop("dynamic_thresholds")
        if self.compressor_model is None:
            result.pop("compressor_model")
        if self.compressor_reasoning is None:
            result.pop("compressor_reasoning")
        return result


@dataclass(frozen=True)
class CompressionSnapshot:
    generation: int
    revision: int
    start: int
    end: int
    step: int
    task: str
    memory: tuple[str, ...]
    raw: tuple[dict[str, Any], ...]
    call_id: str | None = None
    compression_mode: str | None = None
    compression_id: str | None = None


@dataclass(frozen=True)
class BoundaryDecision:
    """A two-stage boundary decision, not a full-memory result when C is None."""
    stage_end: bool
    confidence: float
    reason: str
    C: str | None
    usage: dict[str, Any] | None = None
    latency_seconds: float = 0.0


@dataclass(frozen=True)
class CompressionResult:
    stage_end: bool
    confidence: float
    reason: str
    C: str
    usage: dict[str, Any] | None = None
    latency_seconds: float = 0.0
