"""Unit tests for the three-band verdict logic. No network calls -- this
is exactly the "unit-test the threshold logic against recorded Jev
responses" milestone deliverable from the plan doc's Section 8.

Run with: pytest tests/test_policy.py
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jev_authz.policy import Policy  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _policy(**overrides) -> Policy:
  raw = dict(
      model="typesafe/jev-1.13",
      timeout_seconds=0.8,
      approve_max=0.1,
      deny_min=0.9,
      questions={},
      task_header=None,
      max_body_bytes=1000,
  )
  raw.update(overrides)
  return Policy(**raw)


def _probabilities(fixture_name: str) -> dict[str, float]:
  data = json.loads((FIXTURES / fixture_name).read_text())
  return {k: v["noul"] for k, v in data["answers"].items()}


def test_every_check_clear_permits():
  v = _policy().evaluate(_probabilities("jev_response_permit.json"))
  assert v.permit
  assert v.reason == "every check clear"
  assert not v.alert


def test_any_high_risk_denies():
  v = _policy().evaluate(_probabilities("jev_response_deny.json"))
  assert not v.permit
  assert "could_destroy" in v.reason


def test_middle_band_permits_with_alert():
  v = _policy().evaluate(_probabilities("jev_response_ambiguous.json"))
  assert v.permit
  assert v.alert
  assert "ambiguous" in v.reason
  assert "could_destroy" in v.reason


def test_deny_wins_over_a_simultaneous_middle_band_question():
  v = _policy().evaluate({"off_task": 0.4, "could_destroy": 0.95, "untrusted_input": 0.05})
  assert not v.permit


def test_boundary_values_are_inclusive():
  policy = _policy(approve_max=0.1, deny_min=0.9)
  # Exactly at approve_max counts as clear, not ambiguous.
  clear = policy.evaluate({"a": 0.1, "b": 0.1})
  assert clear.permit and not clear.alert
  # Exactly at deny_min counts as a clear deny.
  denied = policy.evaluate({"a": 0.9, "b": 0.02})
  assert not denied.permit


def test_load_rejects_inverted_thresholds(tmp_path):
  bad = tmp_path / "policy.yaml"
  bad.write_text(
      "approve_max: 0.9\ndeny_min: 0.1\nquestions:\n  x:\n    instructions: 'x'\n")
  try:
    Policy.load(str(bad))
    assert False, "expected ValueError for approve_max >= deny_min"
  except ValueError:
    pass


def test_load_rejects_empty_questions(tmp_path):
  empty = tmp_path / "policy.yaml"
  empty.write_text("approve_max: 0.1\ndeny_min: 0.9\nquestions: {}\n")
  try:
    Policy.load(str(empty))
    assert False, "expected ValueError for no questions"
  except ValueError:
    pass


def test_shipped_policy_yaml_loads():
  # The actual policy.yaml this project ships -- catches typos in the real
  # file, not just a synthetic one.
  repo_root = pathlib.Path(__file__).resolve().parents[1]
  policy = Policy.load(str(repo_root / "policy.yaml"))
  assert set(policy.questions) == {
      "off_task", "could_destroy", "untrusted_input", "ask_first"}
  assert policy.approve_max < policy.deny_min
