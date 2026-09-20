"""Jev probability preservation, abstain, and dispatch eligibility."""

import math

import pytest

from mcp_server_jev_client.providers import (
    SCORED_ACTIONS,
    YAW_CHOICES,
    dispatch_eligibility,
    normalize_decision,
    normalize_scored_decision,
)


def action_probs(choice="rotate", p=0.91, second="wait", second_p=0.05):
    remaining = 1.0 - p - second_p
    others = [name for name in SCORED_ACTIONS if name not in {choice, second}]
    share = remaining / len(others)
    probs = {name: share for name in others}
    probs[choice] = p
    probs[second] = second_p
    return probs


def yaw_probs(choice="90", p=0.9):
    keys = list(YAW_CHOICES)
    rest = (1.0 - p) / (len(keys) - 1)
    return {key: (p if key == choice else rest) for key in keys}


def scored_answers(action="rotate", action_p=0.91, yaw="90", yaw_p=0.88, confidence=0.7, yaw_confidence=0.65):
    payload = {
        "action": {
            "type": "choice",
            "choice": action,
            "probabilities": action_probs(action, action_p),
            "confidence": confidence,
        },
    }
    if action == "rotate":
        payload["yaw_degrees"] = {
            "type": "choice",
            "choice": yaw,
            "probabilities": yaw_probs(yaw, yaw_p),
            "confidence": yaw_confidence,
        }
    return payload


def test_scored_rotate_keeps_action_and_yaw_probabilities_distinct():
    result = normalize_scored_decision(scored_answers(), resolved_model="jev-1.13.0")
    assert result.action == "rotate"
    assert result.arguments.degrees.y == 90
    assert result.selected_action_probability == pytest.approx(0.91)
    assert result.action_probabilities["rotate"] == result.selected_action_probability
    assert result.distribution_confidence == pytest.approx(0.7)
    assert result.selected_yaw_probability == pytest.approx(0.88)
    assert result.yaw_probabilities["90"] == result.selected_yaw_probability
    assert result.yaw_distribution_confidence == pytest.approx(0.65)
    assert result.resolved_model == "jev-1.13.0"
    assert result.selected_action_probability != result.distribution_confidence
    joint = result.selected_action_probability * result.selected_yaw_probability
    assert result.selected_action_probability != pytest.approx(joint)


def test_decide_still_discards_scores_for_observe_compatibility():
    decision = normalize_decision(scored_answers())
    assert decision.action == "rotate"
    assert decision.arguments.degrees.y == 90
    assert not hasattr(decision, "action_probabilities")


def test_missing_confidence_is_left_absent():
    answers = scored_answers(action="wait")
    del answers["action"]["confidence"]
    result = normalize_scored_decision(answers)
    assert result.action == "wait"
    assert result.distribution_confidence is None
    assert result.yaw_probabilities is None


@pytest.mark.parametrize("bad", [
    None,
    {"action": {"type": "choice", "choice": "fly", "probabilities": action_probs()}},
    scored_answers() | {"action": {**scored_answers()["action"], "probabilities": {**action_probs(), "fly": 0.01}}},
    {**scored_answers(), "action": {**scored_answers()["action"], "probabilities": {name: 0.0 for name in SCORED_ACTIONS}}},
    {**scored_answers(), "action": {**scored_answers()["action"], "probabilities": {**action_probs(), "rotate": math.nan}}},
    {**scored_answers(), "action": {**scored_answers()["action"], "probabilities": {**action_probs(), "rotate": 1.2}}},
    {**scored_answers(), "action": {**scored_answers()["action"], "choice": "wait"}},  # wait not top
    {**scored_answers(action="rotate"), "yaw_degrees": {
        "type": "choice", "choice": "91", "probabilities": yaw_probs(),
    }},
])
def test_invalid_score_distributions_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_scored_decision(bad)


def test_tied_top_choice_is_allowed():
    probs = action_probs("rotate", 0.4, "wait", 0.4)
    answers = scored_answers()
    answers["action"]["choice"] = "wait"
    answers["action"]["probabilities"] = probs
    result = normalize_scored_decision(answers)
    assert result.action == "wait"
    assert dispatch_eligibility(result) == "needs_clarification"


def test_low_score_and_abstain_do_not_dispatch():
    low = normalize_scored_decision(scored_answers(action="grab_item", action_p=0.5, yaw="0"))
    assert dispatch_eligibility(low) == "needs_clarification"
    abstain = normalize_scored_decision(scored_answers(action="abstain", action_p=0.92, yaw="0"))
    assert dispatch_eligibility(abstain) == "needs_clarification"


def test_clear_rotate_is_eligible():
    result = normalize_scored_decision(scored_answers())
    assert dispatch_eligibility(result) == "dispatch"


def test_rotate_with_weak_yaw_is_not_dispatched():
    result = normalize_scored_decision(scored_answers(yaw_p=0.5))
    assert dispatch_eligibility(result) == "needs_clarification"
