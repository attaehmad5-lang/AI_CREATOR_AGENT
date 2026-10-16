"""Injectable output-quality scoring with task-specific fail-closed thresholds."""
from __future__ import annotations

from typing import Any

from .safe_numbers import finite_number


class ProviderOutputQualityPolicy:
    """Apply an application-supplied scorer without storing output contents."""

    def __init__(
        self,
        scorer: Any,
        *,
        thresholds: dict[str, float] | None = None,
        default_threshold: float = 0.7,
    ) -> None:
        if not callable(scorer):
            raise ValueError("scorer must be callable")
        default = finite_number(default_threshold, minimum=0)
        if default is None or default > 1:
            raise ValueError("default_threshold must be within [0, 1]")
        if thresholds is not None and not isinstance(thresholds, dict):
            raise ValueError("thresholds must be a mapping")
        checked: dict[str, float] = {}
        for key, value in (thresholds or {}).items():
            if not isinstance(key, str) or not key.strip() or len(key) > 128:
                raise ValueError("invalid quality threshold key")
            number = finite_number(value, minimum=0)
            if number is None or number > 1:
                raise ValueError("quality thresholds must be within [0, 1]")
            checked[key] = number
        self.scorer = scorer
        self.thresholds = checked
        self.default_threshold = default

    def evaluate(self, output_path: Any, *, modality: str, category: str) -> dict[str, Any]:
        if output_path is None:
            return {"allowed": False, "status": "QUALITY_OUTPUT_REQUIRED"}
        if not isinstance(modality, str) or not modality.strip() or len(modality) > 64:
            return {"allowed": False, "status": "INVALID_QUALITY_MODALITY"}
        if not isinstance(category, str) or not category.strip() or len(category) > 64:
            return {"allowed": False, "status": "INVALID_QUALITY_CATEGORY"}
        threshold = self.thresholds.get(
            f"{category}:{modality}",
            self.thresholds.get(category, self.default_threshold),
        )
        try:
            raw = self.scorer(output_path, modality=modality, category=category)
        except Exception as exc:
            return {
                "allowed": False, "status": "QUALITY_SCORING_FAILED",
                "error_type": type(exc).__name__, "threshold": threshold,
            }
        score = raw.get("score") if isinstance(raw, dict) else raw
        normalized = finite_number(score, minimum=0)
        if normalized is None or normalized > 1:
            return {
                "allowed": False, "status": "INVALID_QUALITY_SCORE",
                "threshold": threshold,
            }
        allowed = normalized >= threshold
        return {
            "allowed": allowed,
            "status": "QUALITY_ACCEPTED" if allowed else "QUALITY_BELOW_THRESHOLD",
            "score": normalized,
            "threshold": threshold,
            "category": category,
            "modality": modality,
        }
