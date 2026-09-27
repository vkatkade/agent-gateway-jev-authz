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
"""Minimal client for OpenRouter's Decisions API (Jev, TypeSafe's System One
decision model).

Docs:
  https://openrouter.ai/docs/guides/community/jev
  https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request

This client only speaks Noul (yes/no) questions, which is all the policy in
policy.yaml uses. Jev also supports Choice and Score primitives; add support
for those here if a future policy needs them (see the plan doc, Section 5).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Mapping

import requests

logger = logging.getLogger("jev_authz.jev_client")

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class JevError(RuntimeError):
  """A Decisions call failed, or its response can't be trusted as a verdict
  input. Callers should treat this the same as a definite "unknown" and
  fail closed -- never guess a probability when this is raised."""


class JevClient:
  """Thin wrapper around POST /api/alpha/decisions for Noul questions."""

  def __init__(
      self,
      model: str,
      timeout_seconds: float,
      api_key: str | None = None,
      session: requests.Session | None = None,
  ):
    self._model = model
    self._timeout = timeout_seconds
    self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not self._api_key:
      raise JevError(
          "OPENROUTER_API_KEY is not set (as an env var, or passed to "
          "JevClient directly)")
    # A caller-supplied session (e.g. a fake one in tests) is used as-is;
    # otherwise we own the session and its connection pool.
    self._session = session or requests.Session()

  def ask(
      self,
      state: Any,
      questions: Mapping[str, Mapping[str, str]],
  ) -> dict[str, float]:
    """Sends one Decisions request and returns {question_key: P(yes)}.

    `questions` values must already be Jev question objects, e.g.
    {"off_task": {"type": "noul", "instructions": "..."}}.

    Raises JevError on any network failure, non-200 response, or a response
    missing/malformed for one of the requested questions -- there is no
    silent partial-credit path; either every question gets a valid
    probability, or the whole call is treated as failed.
    """
    payload = {"model": self._model, "state": state, "questions": dict(questions)}
    started = time.monotonic()
    try:
      resp = self._session.post(
          DECISIONS_URL,
          json=payload,
          headers={
              "Authorization": f"Bearer {self._api_key}",
              "Content-Type": "application/json",
          },
          timeout=self._timeout,
      )
    except requests.RequestException as exc:
      raise JevError(f"Decisions request failed: {exc}") from exc
    elapsed_ms = (time.monotonic() - started) * 1000

    if resp.status_code != 200:
      raise JevError(f"Decisions {resp.status_code}: {resp.text[:500]}")

    try:
      data = resp.json()
      answers = data["answers"]
      result: dict[str, float] = {}
      for key in questions:
        prob = float(answers[key]["noul"])
        if not 0.0 <= prob <= 1.0:
          raise ValueError(f"{key}: probability {prob!r} out of [0, 1]")
        result[key] = prob
    except (KeyError, TypeError, ValueError) as exc:
      raise JevError(f"malformed Decisions response: {exc}") from exc

    logger.debug(
        "Jev answered %d question(s) in %.0fms (cost=%s)",
        len(result), elapsed_ms, data.get("usage", {}).get("cost"))
    return result
