"""Tests the Decisions API client against recorded fixtures, via a fake
requests.Session -- no network access, no real OpenRouter API key needed.

Run with: pytest tests/test_jev_client.py
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jev_authz.jev_client import JevClient, JevError  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class _FakeResponse:

  def __init__(self, status_code: int, payload: dict):
    self.status_code = status_code
    self._payload = payload
    self.text = json.dumps(payload)

  def json(self):
    return self._payload


class _FakeSession:
  """Duck-types the one requests.Session method jev_client.py calls."""

  def __init__(self, response: _FakeResponse, exc: Exception | None = None):
    self._response = response
    self._exc = exc
    self.last_call = None

  def post(self, url, json, headers, timeout):  # noqa: A002 - matches requests' signature
    self.last_call = {"url": url, "json": json, "headers": headers, "timeout": timeout}
    if self._exc:
      raise self._exc
    return self._response


def _fixture(name: str) -> dict:
  return json.loads((FIXTURES / name).read_text())


def _questions(fixture: dict) -> dict:
  return {k: {"type": "noul", "instructions": f"is {k}?"} for k in fixture["answers"]}


def test_ask_returns_probabilities_from_fixture():
  fixture = _fixture("jev_response_permit.json")
  session = _FakeSession(_FakeResponse(200, fixture))
  client = JevClient(model="typesafe/jev-1.13", timeout_seconds=0.8,
                      api_key="test-key", session=session)

  result = client.ask({"request": {}}, _questions(fixture))

  assert result == {k: v["noul"] for k, v in fixture["answers"].items()}
  assert session.last_call["headers"]["Authorization"] == "Bearer test-key"
  assert session.last_call["json"]["model"] == "typesafe/jev-1.13"


def test_ask_raises_on_non_200():
  session = _FakeSession(_FakeResponse(500, {"error": "boom"}))
  client = JevClient(model="typesafe/jev-1.13", timeout_seconds=0.8,
                      api_key="test-key", session=session)
  try:
    client.ask({}, {"off_task": {"type": "noul", "instructions": "x"}})
    assert False, "expected JevError"
  except JevError as exc:
    assert "500" in str(exc)


def test_ask_raises_on_missing_answer():
  fixture = _fixture("jev_response_permit.json")
  session = _FakeSession(_FakeResponse(200, fixture))
  client = JevClient(model="typesafe/jev-1.13", timeout_seconds=0.8,
                      api_key="test-key", session=session)
  try:
    client.ask({}, {"a_question_jev_never_answered": {"type": "noul", "instructions": "x"}})
    assert False, "expected JevError"
  except JevError:
    pass


def test_ask_raises_on_network_error():
  import requests
  session = _FakeSession(None, exc=requests.ConnectionError("no route to host"))
  client = JevClient(model="typesafe/jev-1.13", timeout_seconds=0.8,
                      api_key="test-key", session=session)
  try:
    client.ask({}, {"off_task": {"type": "noul", "instructions": "x"}})
    assert False, "expected JevError"
  except JevError as exc:
    assert "no route to host" in str(exc)


def test_missing_api_key_raises_immediately():
  import os
  saved = os.environ.pop("OPENROUTER_API_KEY", None)
  try:
    try:
      JevClient(model="typesafe/jev-1.13", timeout_seconds=0.8)
      assert False, "expected JevError"
    except JevError:
      pass
  finally:
    if saved is not None:
      os.environ["OPENROUTER_API_KEY"] = saved
