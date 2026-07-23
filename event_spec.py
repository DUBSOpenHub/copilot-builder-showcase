"""Portable, validated event configuration for Copilot Builder Showcase."""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, Mapping, Optional


EVENT_SPEC_VERSION = "1.0"

DEFAULT_EVENT_SPEC: Dict[str, Any] = {
    "schema_version": EVENT_SPEC_VERSION,
    "event": {
        "name": "Copilot Builder Showcase",
        "tagline": "Drop the links. Activate the panel. Spotlight the winners.",
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
            "id": "third-place",
            "name": "Third Place — Builder Bronze",
            "emoji": "🥉",
            "tagline": "A podium finish for a build with a memorable spark.",
            "dimensions": [],
            "rank": 3,
            "reason": "This project earned the third-highest complete result across the judging lenses.",
        },
        {
            "id": "second-place",
            "name": "Second Place — Builder Silver",
            "emoji": "🥈",
            "tagline": "One step from the crown with a standout builder story.",
            "dimensions": [],
            "rank": 2,
            "reason": "This project earned the second-highest complete result across the judging lenses.",
        },
        {
            "id": "grand-prize",
            "name": "First Place — Copilot Builder Award",
            "emoji": "🏆",
            "tagline": "The strongest complete build in the showcase.",
            "dimensions": [],
            "rank": 1,
            "reason": "This project delivered the strongest complete showcase across idea, impact, craft, and demo story.",
        },
    ],
    "tie_policy": {
        "mode": "shared-podium",
        "tiebreaker_dimensions": [],
    },
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
        "panel_models": [
            "claude-opus-4.8",
            "gpt-5.6-terra",
            "gemini-3.1-pro-preview",
        ],
        "minimum_panel_size": 3,
        "minimum_distinct_providers": 3,
        "consensus_method": "median",
        "max_parallel_calls": 6,
        "live_time_budget_seconds": 120,
        "live_time_budget_policy": "warn-only",
    },
    "shadow_spec": {
        "enabled": True,
        "criteria_count": 6,
        "ranking_effect": "diagnostic-only",
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


def _model_provider(model_id: str) -> str:
    """Classify model IDs using the same family rule as the freshness gate."""
    normalized = model_id.lower()
    if "/" in normalized:
        return normalized.split("/", 1)[0]
    if normalized.startswith("claude"):
        return "anthropic"
    if normalized.startswith("gpt"):
        return "openai"
    if normalized.startswith("gemini"):
        return "google"
    return normalized.split("-", 1)[0]


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
        award_dimensions = award.get("dimensions")
        if not isinstance(award_dimensions, list):
            raise EventSpecValidationError("EventSpec awards[].dimensions must be a list.")
        for dimension_id in award_dimensions:
            _require_string(dimension_id, "awards[].dimensions[]")
            if dimension_id not in {str(dimension["id"]) for dimension in dimensions}:
                raise EventSpecValidationError(
                    "EventSpec awards[].dimensions must reference configured rubric dimensions."
                )
        distinct_recipient = award.get("distinct_recipient", False)
        if not isinstance(distinct_recipient, bool):
            raise EventSpecValidationError(
                "EventSpec awards[].distinct_recipient must be a boolean when provided."
            )
        if award.get("tie_breaker") not in {None, "overall-ranking"}:
            raise EventSpecValidationError(
                "EventSpec awards[].tie_breaker currently supports only 'overall-ranking'."
            )
        rank = award.get("rank")
        if rank is not None and (
            not isinstance(rank, int) or isinstance(rank, bool) or rank < 1
        ):
            raise EventSpecValidationError(
                "EventSpec awards[].rank must be a positive integer when provided."
            )

    ranks = [award.get("rank") for award in awards if award.get("rank") is not None]
    if len(set(ranks)) != len(ranks):
        raise EventSpecValidationError(
            "EventSpec awards[].rank values must be unique; two awards cannot "
            "both claim the same podium placement."
        )

    tie_policy = spec.get("tie_policy")
    if not isinstance(tie_policy, Mapping):
        raise EventSpecValidationError("EventSpec requires a 'tie_policy' object.")
    tie_mode = tie_policy.get("mode")
    allowed_tie_modes = {
        "shared-podium",
        "sealed-tiebreaker",
        "human-resolution",
    }
    if tie_mode not in allowed_tie_modes:
        raise EventSpecValidationError(
            "EventSpec tie_policy.mode must be 'shared-podium', "
            "'sealed-tiebreaker', or 'human-resolution'."
        )
    tiebreaker_dimensions = tie_policy.get("tiebreaker_dimensions")
    if not isinstance(tiebreaker_dimensions, list):
        raise EventSpecValidationError(
            "EventSpec tie_policy.tiebreaker_dimensions must be a list."
        )
    if len(set(tiebreaker_dimensions)) != len(tiebreaker_dimensions):
        raise EventSpecValidationError(
            "EventSpec tie_policy.tiebreaker_dimensions must not contain duplicates."
        )
    dimension_ids = {str(dimension["id"]) for dimension in dimensions}
    for dimension_id in tiebreaker_dimensions:
        _require_string(dimension_id, "tie_policy.tiebreaker_dimensions[]")
        if dimension_id not in dimension_ids:
            raise EventSpecValidationError(
                "EventSpec tie_policy.tiebreaker_dimensions must reference "
                "configured rubric dimensions."
            )
    if tie_mode == "sealed-tiebreaker" and not tiebreaker_dimensions:
        raise EventSpecValidationError(
            "EventSpec sealed-tiebreaker policy requires at least one "
            "tiebreaker dimension."
        )
    if tie_mode != "sealed-tiebreaker" and tiebreaker_dimensions:
        raise EventSpecValidationError(
            "EventSpec tiebreaker dimensions are only valid with "
            "tie_policy.mode='sealed-tiebreaker'."
        )

    accessibility = spec.get("accessibility", {})
    if not isinstance(accessibility, Mapping):
        raise EventSpecValidationError("EventSpec accessibility must be an object.")
    allowed_accessibility_keys = {"high_contrast", "reduced_motion"}
    unknown_accessibility_keys = set(accessibility) - allowed_accessibility_keys
    if unknown_accessibility_keys:
        raise EventSpecValidationError(
            "EventSpec accessibility contains unsupported key(s): "
            + ", ".join(sorted(unknown_accessibility_keys))
            + ". Supported keys are 'high_contrast' and 'reduced_motion'."
        )
    for key in allowed_accessibility_keys:
        if key in accessibility and not isinstance(accessibility[key], bool):
            raise EventSpecValidationError(
                f"EventSpec accessibility.{key} must be a boolean."
            )

    model_policy = spec.get("model_policy")
    if not isinstance(model_policy, Mapping):
        raise EventSpecValidationError("EventSpec requires a 'model_policy' object.")
    panel_models = model_policy.get("panel_models")
    if not isinstance(panel_models, list) or not panel_models:
        raise EventSpecValidationError(
            "EventSpec model_policy.panel_models must contain at least one model id."
        )
    if len(set(panel_models)) != len(panel_models):
        raise EventSpecValidationError(
            "EventSpec model_policy.panel_models must not contain duplicate model ids."
        )
    for model_id in panel_models:
        _require_string(model_id, "model_policy.panel_models[]")
    _require_string(
        model_policy.get("preferred_model"), "model_policy.preferred_model"
    )
    if model_policy.get("policy_mode", "strict") not in {"strict", "permissive"}:
        raise EventSpecValidationError(
            "EventSpec model_policy.policy_mode must be 'strict' or 'permissive'. "
            "A misspelled or mismatched value would silently fall back to "
            "permissive behavior instead of blocking a strict event."
        )
    if model_policy.get("required_tier", "standard") not in {"standard", "premium"}:
        raise EventSpecValidationError(
            "EventSpec model_policy.required_tier must be 'standard' or 'premium'."
        )
    # Kept in sync with builder_showcase._REASONING_LEVELS; duplicated here
    # (rather than imported) to avoid a circular import between the two modules.
    valid_reasoning_levels = {"low", "medium", "high", "xhigh"}
    required_reasoning = model_policy.get("required_reasoning", "high")
    if (
        not isinstance(required_reasoning, str)
        or required_reasoning.lower() not in valid_reasoning_levels
    ):
        raise EventSpecValidationError(
            "EventSpec model_policy.required_reasoning must be one of "
            "'low', 'medium', 'high', or 'xhigh'."
        )
    minimum_panel_size = model_policy.get("minimum_panel_size", 1)
    if (
        not isinstance(minimum_panel_size, int)
        or isinstance(minimum_panel_size, bool)
        or minimum_panel_size < 1
        or minimum_panel_size > len(panel_models)
    ):
        raise EventSpecValidationError(
            "EventSpec model_policy.minimum_panel_size must be a positive integer "
            "no larger than the configured panel."
        )
    minimum_distinct_providers = model_policy.get("minimum_distinct_providers", 1)
    if (
        not isinstance(minimum_distinct_providers, int)
        or isinstance(minimum_distinct_providers, bool)
        or minimum_distinct_providers < 1
        or minimum_distinct_providers
        > len({_model_provider(model_id) for model_id in panel_models})
    ):
        raise EventSpecValidationError(
            "EventSpec model_policy.minimum_distinct_providers must be a positive "
            "integer no larger than the configured provider-family count."
        )
    if model_policy.get("consensus_method", "median") != "median":
        raise EventSpecValidationError(
            "EventSpec model_policy.consensus_method currently supports only 'median'."
        )
    max_parallel_calls = model_policy.get("max_parallel_calls", 1)
    if (
        not isinstance(max_parallel_calls, int)
        or isinstance(max_parallel_calls, bool)
        or not 1 <= max_parallel_calls <= 32
    ):
        raise EventSpecValidationError(
            "EventSpec model_policy.max_parallel_calls must be an integer from 1 through 32."
        )
    live_time_budget_seconds = model_policy.get("live_time_budget_seconds", 30)
    if (
        not isinstance(live_time_budget_seconds, int)
        or isinstance(live_time_budget_seconds, bool)
        or not 30 <= live_time_budget_seconds <= 3600
    ):
        raise EventSpecValidationError(
            "EventSpec model_policy.live_time_budget_seconds must be an integer "
            "from 30 through 3600."
        )
    if model_policy.get("live_time_budget_policy", "warn-only") != "warn-only":
        raise EventSpecValidationError(
            "EventSpec model_policy.live_time_budget_policy currently supports only "
            "'warn-only' so strict panels are never silently reduced."
        )

    shadow_spec = spec.get("shadow_spec")
    if not isinstance(shadow_spec, Mapping):
        raise EventSpecValidationError("EventSpec requires a 'shadow_spec' object.")
    if not isinstance(shadow_spec.get("enabled"), bool):
        raise EventSpecValidationError("EventSpec shadow_spec.enabled must be a boolean.")
    criteria_count = shadow_spec.get("criteria_count", 6)
    if (
        not isinstance(criteria_count, int)
        or isinstance(criteria_count, bool)
        or not 6 <= criteria_count <= 8
    ):
        raise EventSpecValidationError(
            "EventSpec shadow_spec.criteria_count must be an integer from 6 through 8."
        )
    if shadow_spec.get("ranking_effect") != "diagnostic-only":
        raise EventSpecValidationError(
            "EventSpec shadow_spec.ranking_effect must be 'diagnostic-only'."
        )


def event_spec_to_rubric(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Convert a resolved EventSpec into the engine's legacy rubric snapshot."""
    return {
        "version": str(spec.get("schema_version", EVENT_SPEC_VERSION)),
        "rubric": copy.deepcopy(spec["rubric"]),
        "judge_archetypes": copy.deepcopy(spec["review_lenses"]),
        "tie_policy": copy.deepcopy(spec["tie_policy"]),
        "tone_policy": copy.deepcopy(spec["tone_policy"]),
        "freshness_gate": copy.deepcopy(spec["model_policy"]),
        "shadow_spec": copy.deepcopy(spec["shadow_spec"]),
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
    legacy_model_policy = rubric.get("freshness_gate")
    if isinstance(legacy_model_policy, Mapping):
        spec["model_policy"] = _merge(spec["model_policy"], legacy_model_policy)
    legacy_shadow_spec = rubric.get("shadow_spec")
    if isinstance(legacy_shadow_spec, Mapping):
        spec["shadow_spec"] = _merge(spec["shadow_spec"], legacy_shadow_spec)
    legacy_tie_policy = rubric.get("tie_policy")
    if isinstance(legacy_tie_policy, Mapping):
        spec["tie_policy"] = _merge(spec["tie_policy"], legacy_tie_policy)
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
