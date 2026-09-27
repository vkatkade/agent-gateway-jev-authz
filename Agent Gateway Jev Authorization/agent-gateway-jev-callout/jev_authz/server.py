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
"""Agent Gateway custom CONTENT_AUTHZ extension, backed by Jev.

Buffers the full request body across ext_proc FULL_DUPLEX_STREAMED chunks,
asks Jev (TypeSafe's System One decision model, via OpenRouter's Decisions
API) a small set of Noul yes/no policy questions about it, and turns the
returned probabilities into a Permit or Deny verdict for Agent Gateway to
enforce. See the implementation plan doc (Sections 3-6) for the design this
implements, and README.md for how to run and deploy it.

Why withhold body chunks instead of passing them through:
  FULL_DUPLEX_STREAMED forwards chunks to the destination backend as they
  arrive, without waiting for us. If we let each chunk through unchanged as
  it arrives, a Deny verdict on the *last* chunk would arrive too late --
  earlier bytes may already be on their way to the backend. So every
  non-final chunk is withheld (`clear_body=True`); nothing reaches the
  backend until we've reassembled the whole body and reached a verdict.
  Because of that, a Permit verdict has to re-send the *entire* body on the
  final chunk, not just that last piece -- see `_permit_response` below.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from envoy.service.ext_proc.v3.external_processor_pb2 import (
    BodyResponse,
    HttpBody,
    HttpHeaders,
    ImmediateResponse,
)
from envoy.type.v3.http_status_pb2 import StatusCode
from grpc import ServicerContext

from extproc.service.callout_server import CalloutServer
from extproc.service.callout_tools import add_body_mutation
from extproc.service.command_line_tools import add_command_line_args

from jev_authz.jev_client import JevClient, JevError
from jev_authz.policy import Policy, Verdict
from jev_authz.state import build_state, headers_to_dict

logger = logging.getLogger("jev_authz")


class JevAuthzCalloutServer(CalloutServer):
  """Delegates Agent Gateway CONTENT_AUTHZ decisions to Jev."""

  def __init__(self, policy_path: str = "policy.yaml", **kwargs):
    # CalloutServer.__init__ reads TLS/plaintext/health-check args and wires
    # up the underlying gRPC server; see extproc/service/callout_server.py.
    super().__init__(**kwargs)
    self._policy = Policy.load(policy_path)
    self._jev = JevClient(
        model=self._policy.model,
        timeout_seconds=self._policy.timeout_seconds,
    )
    # Per-gRPC-stream scratch space. A synchronous streaming RPC is handled
    # start to finish by a single worker thread (see _GRPCCalloutService.
    # Process in callout_server.py), so thread-local storage needs no
    # locking, and is safely overwritten at the start of every new stream --
    # there is no cross-request leakage even though threads are reused.
    self._local = threading.local()
    logger.info(
        "Loaded policy from %s: %d question(s), approve_max=%.2f, "
        "deny_min=%.2f, model=%s", policy_path, len(self._policy.questions),
        self._policy.approve_max, self._policy.deny_min, self._policy.model)

  # -- ext_proc hooks ---------------------------------------------------

  def on_request_headers(self, headers: HttpHeaders, context: ServicerContext):
    del context  # unused; state lives on self._local for this thread
    self._local.headers = headers_to_dict(headers)
    self._local.chunks = []
    self._local.started = time.monotonic()
    return None  # observe only; no header mutation

  def on_request_body(self, body: HttpBody, context: ServicerContext):
    del context
    chunks = getattr(self._local, "chunks", None)
    if chunks is None:
      # A body event with no preceding request_headers event we saw --
      # shouldn't happen under FULL_DUPLEX_STREAMED, but start a buffer
      # rather than crash.
      chunks = self._local.chunks = []
      self._local.headers = getattr(self._local, "headers", {})
      self._local.started = time.monotonic()
    chunks.append(body.body)

    if not body.end_of_stream:
      return add_body_mutation(clear_body=True)  # withhold; see module docstring

    full_body = b"".join(chunks)
    headers = self._local.headers
    verdict = self._decide(headers, full_body)
    latency_ms = round((time.monotonic() - self._local.started) * 1000, 1)
    self._audit_log(headers, full_body, verdict, latency_ms)

    if verdict.permit:
      return self._permit_response(full_body)
    return self._deny_response(verdict)

  # -- decision -----------------------------------------------------------

  def _decide(self, headers: dict, body: bytes) -> Verdict:
    try:
      state = build_state(self._policy, headers, body)
    except ValueError as exc:
      logger.warning("state build failed, denying: %s", exc)
      return Verdict.deny(reason=f"could not evaluate request: {exc}", probabilities={})

    try:
      answers = self._jev.ask(state, self._policy.questions)
    except JevError as exc:
      # Fail closed on our own errors too, not just on Envoy's connectivity
      # failOpen: false. See the plan doc's Open questions & risks.
      logger.error("Jev call failed, failing closed: %s", exc)
      return Verdict.deny(reason=f"policy engine unavailable: {exc}", probabilities={})

    return self._policy.evaluate(answers)

  # -- response construction ----------------------------------------------

  def _permit_response(self, full_body: bytes) -> BodyResponse:
    """Releases the whole reassembled body on the final chunk.

    Built directly rather than via callout_tools.add_body_mutation(), which
    takes body as `str` and re-encodes it -- fine for text, but this keeps
    the original bytes exactly as received.
    """
    resp = BodyResponse()
    resp.response.body_mutation.body = full_body
    return resp

  def _deny_response(self, verdict: Verdict) -> ImmediateResponse:
    payload = json.dumps({
        "decision": "deny",
        "reason": verdict.reason,
        "probabilities": verdict.probabilities,
    }).encode("utf-8")
    resp = ImmediateResponse()
    resp.status.code = StatusCode.Forbidden
    resp.body = payload
    resp.details = "denied by the Jev authorization callout"
    return resp

  def _audit_log(self, headers: dict, body: bytes, verdict: Verdict, latency_ms: float) -> None:
    # One structured line per decision, permit and deny alike -- see the
    # plan doc's success criteria ("every decision is logged with its
    # reason and per-question probabilities").
    logger.info(json.dumps({
        "event": "jev_authz_decision",
        "verdict": "permit" if verdict.permit else "deny",
        "alert": verdict.alert,
        "reason": verdict.reason,
        "probabilities": verdict.probabilities,
        "latency_ms": latency_ms,
        "method": headers.get(":method"),
        "path": headers.get(":path"),
        "body_bytes": len(body),
    }))


if __name__ == "__main__":
  import os

  args = add_command_line_args().parse_args()
  logging.basicConfig(level=logging.INFO)
  policy_path = os.environ.get("JEV_POLICY_PATH", "policy.yaml")
  JevAuthzCalloutServer(policy_path=policy_path, **vars(args)).run()
