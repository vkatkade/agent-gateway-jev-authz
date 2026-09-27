# Copyright 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Turns ext_proc HttpHeaders + a reassembled request body into:

  * a plain `dict[str, str]` of headers the rest of the server reads, and
  * the `state` object sent to Jev as evidence for its policy questions.

Kept separate from server.py so the state shape can be unit-tested (see
tests/test_state.py) without any protobuf or gRPC machinery involved.
"""
from __future__ import annotations

import json
from typing import Any

from envoy.service.ext_proc.v3.external_processor_pb2 import HttpHeaders

from jev_authz.policy import Policy

# Fixed context every Jev question is implicitly judged against. Keep this
# short: it counts against the 32,000-token state budget on every call.
POLICY_TEXT = (
    "Requests arrive through Google Cloud Agent Gateway from agents, "
    "MCP clients, and the tools those agents call. Judge each request on "
    "its own merits from `request` and `task`; do not assume earlier turns "
    "in the same session were already checked.")


def headers_to_dict(headers: HttpHeaders) -> dict[str, str]:
  """Flattens an ext_proc HttpHeaders message to {lowercase key: value}.

  A repeated header keeps only its last value -- acceptable for a POC; a
  production version should preserve repeats (see the plan doc's Open
  questions & risks for what's deliberately deferred).
  """
  result: dict[str, str] = {}
  for header in headers.headers.headers:
    if header.raw_value:
      value = header.raw_value.decode("utf-8", "replace")
    else:
      value = header.value
    result[header.key.lower()] = value
  return result


def build_state(policy: Policy, headers: dict[str, str], body: bytes) -> dict[str, Any]:
  """Builds the `state` dict passed to Jev's Decisions API.

  Raises ValueError if the body is too large to reason about (see
  `max_body_bytes` in policy.yaml) -- callers should treat that the same as
  any other state-build failure and fail closed.
  """
  if len(body) > policy.max_body_bytes:
    raise ValueError(
        f"body is {len(body)} bytes, over the {policy.max_body_bytes}-byte "
        "limit in policy.yaml")

  request: dict[str, Any] = {
      "method": headers.get(":method"),
      "path": headers.get(":path"),
      "content_type": headers.get("content-type"),
  }
  if body:
    try:
      request["body"] = json.loads(body)
    except json.JSONDecodeError:
      # Not JSON (or not valid JSON) -- still worth sending as text so Jev
      # has something to read, e.g. a plain-text prompt.
      request["body"] = body.decode("utf-8", "replace")
  else:
    request["body"] = None

  task = headers.get(policy.task_header) if policy.task_header else None

  return {
      "policy": POLICY_TEXT,
      "task": task,
      "request": request,
  }
