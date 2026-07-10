"""Portable, validated event configuration for Hackathon Judge."""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, Mapping, Optional


EVENT_SPEC_VERSION = "1.0"

DEFAULT_EVENT_SPEC: Dict[str, Any] = {
    "schema_version": EVENT_SPEC_VERSION,
    "event": {
        "name": "Hackathon Judge",
        "tagline": "Projects in. Fair judging. A shared celebration.",
    },
    "rubric": {
        "dimensions": [
            {
                "id": "innovation",
                "name": "Innovation",
                "weight": 0.30,
                "max_score": 10,
                "description": "Originality and a thoughtful approach to the challenge",
            },
            {
                "id": "impact",
                "name": "Potential Impact",
                "weight": 0.25,
                "max_score": 10,
                "description": "Value for people using the project",
            },
            {
                "id": "execution",
                "name": "Technical Execution",
                "weight": 0.25,
                "max_score": 10,
                "description": "Quality, completeness, and craft",
            },
            {
                "id": "presentation",
                "name": "Clarity and Demo",
                "weight": 0.20,
                "max_score": 10,
                "description": "How clearly the project and its value are communicated",
            },
        ]
    },
    "review_lenses": [
        {
            "id": "innovation",
            "name": "Innovation lens",
            "focus": "originality, problem framing, and useful creative choices",
        },
        {
            "id": "craft",
            "name": "Build quality lens",
            "focus": "technical execution, completeness, and thoughtful implementation",
        },
        {
            "id": "impact",
            "name": "Impact lens",
            "focus": "user value, feasibility, and the strength of the demo story",
        },
    ],
    "awards": [
        {
            "id": "innovation",
            "name": "Innovation Award",
            "emoji": "✨",
            "tagline": "For a project with a memorable new idea.",
            "dimensions": ["innovation"],
            "reason": "This project paired a distinctive idea with a compelling problem to solve.",
        },
        {
            "id": "craft",
            "name": "Build Quality Award",
            "emoji": "🛠️",
            "tagline": "For thoughtful execution and a strong working experience.",
            "dimensions": ["execution", "presentation"],
            "reason": "This project showed care in its implementation and the way it was presented.",
        },
        {
            "id": "grand-prize",
            "name": "Hackathon Grand Prize",
            "emoji": "🏆",
            "tagline": "For the strongest overall project story.",
            "dimensions": [],
            "reason": "This project delivered the strongest overall result across the event rubric.",
        },
    ],
    "presentation": {
        "audience_view": "masked-until-awards",
        "screen_share": {
            "hide_scores_until": "awarded",
            "show_progress": True,
            "show_spotlights": True,
        },
    },
    "privacy": {
        "visibility": "internal",
        "hide_scores_until": "awarded",
    },
    "accessibility": {
        "high_contrast": True,
        "reduced_motion": False,
    },
    "model_policy": {
        "policy_mode": "strict",
        "preferred_model": "claude-opus-4.8",
        "required_tier": "premium",
        "required_reasoning": "high",
    },
    "tone_policy": {
        "banned_phrases": [],
        "extra_banned_phrases": [],
    },
    "submission_size_cap_bytes": 5 * 1024 * 1024,
}


class EventSpecValidationError(ValueError):
    """Raised when an event configuration cannot produce a safe run snapshot."""


def is_event_spec(document: Mapping[str, Any]) -> bool:
    """Return whether a JSON document uses the EventSpec shape."""
    return any(key in document for key in ("event", "review_lenses", "awards", "presentation"))


def _merge(default: Any, override: Any) -> Any:
    if isinstance(default, dict) and isinstance(override, Mapping):
        merged = copy.deepcopy(default)
        for key, value in override.items():
            merged[key] = _merge(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(override)


def _require_string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EventSpecValidationError(f"EventSpec field '{field}' must be a non-empty string.")


def _validate_unique_ids(items: Iterable[Mapping[str, Any]], field: str) -> None:
    seen = set()
    for item in items:
        item_id = item.get("id")
        _require_string(item_id, f"{field}[].id")
        if item_id in seen:
            raise EventSpecValidationError(f"EventSpec {field} contains duplicate id '{item_id}'.")
        seen.add(item_id)


def validate_event_spec(spec: Mapping[str, Any]) -> None:
    """Validate the subset of EventSpec that affects persistent event behavior."""
    event = spec.get("event")
    if not isinstance(event, Mapping):
        raise EventSpecValidationError("EventSpec requires an 'event' object.")
    _require_string(event.get("name"), "event.name")
    _require_string(event.get("tagline"), "event.tagline")

    rubric = spec.get("rubric")
    if not isinstance(rubric, Mapping):
        raise EventSpecValidationError("EventSpec requires a 'rubric' object.")
    dimensions = rubric.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise EventSpecValidationError("EventSpec rubric requires at least one dimension.")
    _validate_unique_ids(dimensions, "rubric.dimensions")
    total_weight = 0.0
    for dimension in dimensions:
        _require_string(dimension.get("name"), "rubric.dimensions[].name")
        weight = dimension.get("weight")
        max_score = dimension.get("max_score")
        if not isinstance(weight, (int, float)) or weight < 0:
            raise EventSpecValidationError("Each rubric dimension requires a non-negative numeric weight.")
        if not isinstance(max_score, (int, float)) or max_score <= 0:
            raise EventSpecValidationError("Each rubric dimension requires a positive max_score.")
        total_weight += float(weight)
    if abs(total_weight - 1.0) > 0.001:
        raise EventSpecValidationError(
            f"EventSpec rubric dimension weights must sum to 1.0 (got {total_weight:.4f})."
        )

    lenses = spec.get("review_lenses")
    if not isinstance(lenses, list) or not lenses:
        raise EventSpecValidationError("EventSpec requires at least one neutral review lens.")
    _validate_unique_ids(lenses, "review_lenses")
    for lens in lenses:
        _require_string(lens.get("name"), "review_lenses[].name")
        _require_string(lens.get("focus"), "review_lenses[].focus")

    awards = spec.get("awards")
    if not isinstance(awards, list) or not awards:
        raise EventSpecValidationError("EventSpec requires at least one award.")
    _validate_unique_ids(awards, "awards")
    for award in awards:
        _require_string(award.get("name"), "awards[].name")
        _require_string(award.get("emoji"), "awards[].emoji")
        _require_string(award.get("tagline"), "awards[].tagline")
        if not isinstance(award.get("dimensions"), list):
            raise EventSpecValidationError("EventSpec awards[].dimensions must be a list.")


def event_spec_to_rubric(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Convert a resolved EventSpec into the engine's legacy rubric snapshot."""
    return {
        "version": str(spec.get("schema_version", EVENT_SPEC_VERSION)),
        "rubric": copy.deepcopy(spec["rubric"]),
        "judge_archetypes": copy.deepcopy(spec["review_lenses"]),
        "tone_policy": copy.deepcopy(spec["tone_policy"]),
        "freshness_gate": copy.deepcopy(spec["model_policy"]),
        "submission_size_cap_bytes": spec["submission_size_cap_bytes"],
    }


def legacy_rubric_to_event_spec(rubric: Mapping[str, Any]) -> Dict[str, Any]:
    """Wrap a legacy rubric in neutral event defaults without changing its scoring."""
    spec = copy.deepcopy(DEFAULT_EVENT_SPEC)
    spec["rubric"] = copy.deepcopy(rubric.get("rubric", spec["rubric"]))
    spec["review_lenses"] = copy.deepcopy(
        rubric.get("judge_archetypes", spec["review_lenses"])
    )
    spec["tone_policy"] = copy.deepcopy(rubric.get("tone_policy", spec["tone_policy"]))
    spec["model_policy"] = copy.deepcopy(rubric.get("freshness_gate", spec["model_policy"]))
    spec["submission_size_cap_bytes"] = rubric.get(
        "submission_size_cap_bytes", spec["submission_size_cap_bytes"]
    )
    return spec


def resolve_event_spec(
    event_document: Optional[Mapping[str, Any]] = None,
    legacy_rubric: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resolve a new EventSpec or wrap a legacy rubric in neutral defaults.

    The result is fully expanded so a run snapshot remains reproducible even if
    the source configuration later changes.
    """
    legacy = legacy_rubric or {}
    if event_document and is_event_spec(event_document):
        base = legacy_rubric_to_event_spec(legacy) if legacy else copy.deepcopy(DEFAULT_EVENT_SPEC)
        spec = _merge(base, event_document)
    elif event_document:
        spec = legacy_rubric_to_event_spec(event_document)
    else:
        spec = legacy_rubric_to_event_spec(legacy) if legacy else copy.deepcopy(DEFAULT_EVENT_SPEC)

    validate_event_spec(spec)
    return spec
