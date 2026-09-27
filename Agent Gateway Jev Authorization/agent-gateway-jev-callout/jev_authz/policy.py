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
"""Loads the Jev policy (Noul questions + thresholds) from policy.yaml and
turns Jev's answers into a Permit/Deny verdict.

The three-band decision here matches the plan doc's Section 5 ("Jev decision
engine integration") as resolved in Open questions & risks: every check
clear -> permit; any check clearly true -> deny; anything in between ->
permit with an async alert, because this callout has no synchronous "pause
for a human" path. Change the *threshold numbers* in policy.yaml freely;
changing the *shape* of these three bands is a product decision, and the
plan doc, not this file, is where that decision is recorded.
"""
from __future__ import annotations

import dataclasses
from typing import Mapping

import yaml


@dataclasses.dataclass(frozen=True)
class Verdict:
  permit: bool
  reason: str
  probabilities: dict[str, float]
  alert: bool = False  # True for the "ambiguous, permitted anyway" band

  @classmethod
  def deny(cls, reason: str, probabilities: Mapping[str, float]) -> "Verdict":
    return cls(permit=False, reason=reason, probabilities=dict(probabilities))

  @classmethod
  def permit_clear(cls, probabilities: Mapping[str, float]) -> "Verdict":
    return cls(permit=True, reason="every check clear",
               probabilities=dict(probabilities))

  @classmethod
  def permit_with_alert(cls, reason: str, probabilities: Mapping[str, float]) -> "Verdict":
    return cls(permit=True, reason=reason, probabilities=dict(probabilities),
               alert=True)


@dataclasses.dataclass(frozen=True)
class Policy:
  model: str
  timeout_seconds: float
  approve_max: float   # a question at or below this is "clearly false" (low risk)
  deny_min: float       # a question at or above this is "clearly true" (high risk)
  questions: dict[str, dict[str, str]]
  task_header: str | None
  max_body_bytes: int

  @classmethod
  def load(cls, path: str) -> "Policy":
    with open(path, "r", encoding="utf-8") as fh:
      raw = yaml.safe_load(fh)

    raw_questions = raw.get("questions") or {}
    if not raw_questions:
      raise ValueError(f"{path}: no questions defined under `questions:`")
    questions = {
        key: {"type": "noul", "instructions": value["instructions"]}
        for key, value in raw_questions.items()
    }

    approve_max = float(raw.get("approve_max", 0.1))
    deny_min = float(raw.get("deny_min", 0.9))
    if not 0.0 <= approve_max < deny_min <= 1.0:
      raise ValueError(
          f"{path}: need 0 <= approve_max < deny_min <= 1, got "
          f"approve_max={approve_max}, deny_min={deny_min}")

    return cls(
        model=raw.get("model", "typesafe/jev-1.13"),
        timeout_seconds=float(raw.get("timeout_seconds", 0.8)),
        approve_max=approve_max,
        deny_min=deny_min,
        questions=questions,
        task_header=raw.get("task_header") or None,
        max_body_bytes=int(raw.get("max_body_bytes", 200_000)),
    )

  def evaluate(self, probabilities: Mapping[str, float]) -> Verdict:
    probs = dict(probabilities)

    failed = {k: p for k, p in probs.items() if p >= self.deny_min}
    if failed:
      reason = "failed " + ", ".join(f"{k}={v:.2f}" for k, v in sorted(failed.items()))
      return Verdict.deny(reason, probs)

    if all(p <= self.approve_max for p in probs.values()):
      return Verdict.permit_clear(probs)

    borderline = {k: p for k, p in probs.items() if p > self.approve_max}
    reason = (
        "ambiguous (" +
        ", ".join(f"{k}={v:.2f}" for k, v in sorted(borderline.items())) +
        "); permitted with an async alert")
    return Verdict.permit_with_alert(reason, probs)
