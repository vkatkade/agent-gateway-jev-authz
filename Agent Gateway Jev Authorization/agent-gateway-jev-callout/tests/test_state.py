"""Tests for header flattening and Jev state construction. No protobuf
machinery needed beyond constructing HttpHeaders directly.

Run with: pytest tests/test_state.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from envoy.config.core.v3.base_pb2 import HeaderMap, HeaderValue  # noqa: E402
from envoy.service.ext_proc.v3.external_processor_pb2 import HttpHeaders  # noqa: E402

from jev_authz.policy import Policy  # noqa: E402
from jev_authz.state import build_state, headers_to_dict  # noqa: E402


def _headers(pairs: list[tuple[str, str]]) -> HttpHeaders:
  header_map = HeaderMap(headers=[
      HeaderValue(key=k, raw_value=v.encode()) for k, v in pairs
  ])
  return HttpHeaders(headers=header_map)


def _policy(**overrides) -> Policy:
  raw = dict(model="m", timeout_seconds=0.8, approve_max=0.1, deny_min=0.9,
             questions={}, task_header="x-agent-task", max_body_bytes=1000)
  raw.update(overrides)
  return Policy(**raw)


def test_headers_to_dict_lowercases_keys():
  h = _headers([(":method", "POST"), ("Content-Type", "application/json")])
  d = headers_to_dict(h)
  assert d[":method"] == "POST"
  assert d["content-type"] == "application/json"


def test_build_state_parses_json_body():
  policy = _policy()
  headers = {":method": "POST", ":path": "/mcp", "content-type": "application/json"}
  state = build_state(policy, headers, b'{"tool": "delete_file", "path": "/tmp/x"}')
  assert state["request"]["method"] == "POST"
  assert state["request"]["body"] == {"tool": "delete_file", "path": "/tmp/x"}


def test_build_state_falls_back_to_text_for_non_json_body():
  policy = _policy()
  state = build_state(policy, {}, b"not json")
  assert state["request"]["body"] == "not json"


def test_build_state_reads_task_header():
  policy = _policy(task_header="x-agent-task")
  headers = {"x-agent-task": "fix the failing unit test"}
  state = build_state(policy, headers, b"{}")
  assert state["task"] == "fix the failing unit test"


def test_build_state_skips_task_header_when_unconfigured():
  policy = _policy(task_header=None)
  headers = {"x-agent-task": "should be ignored"}
  state = build_state(policy, headers, b"{}")
  assert state["task"] is None


def test_build_state_rejects_oversized_body():
  policy = _policy(max_body_bytes=10)
  try:
    build_state(policy, {}, b"x" * 11)
    assert False, "expected ValueError"
  except ValueError:
    pass
