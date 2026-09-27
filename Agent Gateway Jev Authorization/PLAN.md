# Agent Gateway Custom Authz via Jev — Implementation Plan

Sep 27, 2026 · @Vaibhav

## Overview & goals

This plan is for a **Service Extension callout** that plugs into Google Cloud Agent Gateway's `CONTENT_AUTHZ` custom authorization extension point and delegates the permit/deny decision to **Jev**, TypeSafe's structured decision model available on OpenRouter (`typesafe/jev-1.13`). The callout inspects each request's headers and body as it passes through Agent Gateway, builds a **state** object (the request, the caller's declared task, and the governing policy text), sends one or more typed **Noul** (yes/no) questions to Jev, and turns the returned probabilities into a `Permit` or `Deny` verdict that Agent Gateway enforces synchronously.

This is the same delegation pattern Agent Gateway already uses for IAP (`REQUEST_AUTHZ`) and Model Armor (`CONTENT_AUTHZ`) — Jev becomes a **third custom authorization engine**, suited to semantic checks that can't be expressed as CEL matchers or static IAM rules. A natural starting set of checks mirrors the categories the [Jev Overseer demo](https://openrouter.ai/labs/jev/overseer) already runs on agent tool calls: off task, could destroy, untrusted input, ask first.

**Success criteria**

- The callout's own decision latency (state build + Jev round trip) fits inside the authorization extension's configured `timeout` (documented examples use `1s`), leaving headroom for gateway and network overhead.
- Policies (the Jev questions and thresholds) are configuration, not code — changing a policy shouldn't require rebuilding the callout image.
- Every decision is logged with its reason and per-question probabilities, for audit.
- The extension fails closed (`failOpen: false`) by default, with an explicit, agreed answer for what happens when Jev, OpenRouter, or the network is unavailable.
- The design fits Agent Gateway's stated limits: up to four custom authorization policies per egress gateway, and at most one `CONTENT_AUTHZ` policy per ingress gateway.

**Scope note.** This plan targets a working **proof of concept** first — no VPC, DNS peering, or certificate management required, since an OpenRouter API key is the only credential a POC needs. Section 7 gives the simplified POC deployment path; the VPC/TLS hardening Google recommends for production is called out separately, in Section 3 and in Open questions & risks, and lands as its own milestone rather than a POC blocker.

## High-level architecture

Jev's verdict — not the request's type alone — decides whether Agent Gateway forwards the call or returns an immediate deny.

**Diagram (request flow: gateway → callout → Jev → decision):** Client → Agent Gateway → `Callout: ext_proc` → `Verdict?` → on **permit**, → Destination → Response to client. The callout also calls **Jev via OpenRouter** (labeled "Jev call") before reaching the verdict. On **deny**, the path returns to Agent Gateway as `deny → 403`, never reaching the destination.

The interactive version of this diagram is in the live plan doc: https://claude.ai/code/artifact/eaeeab40-3b5b-4633-a9ee-2f5fb6fb0374 (this export is a static markdown snapshot; the doc itself also has the two decision threads referenced in Open questions & risks).

The gateway opens a gRPC `ext_proc` stream to our callout for every matched request. The callout reassembles the body, calls Jev's Decisions API with the governing policy as typed questions, and turns the returned probabilities into a verdict — permitting the request onward, or ending the stream early with an `immediate_response` deny. Section 4 covers the `ext_proc` mechanics; Section 5 covers the Jev call itself.

## Google Cloud Agent Gateway: custom authorization delegation

Agent Gateway authorization policies use **policy profiles**:

| Profile | Sees | Typical use |
| --- | --- | --- |
| `REQUEST_AUTHZ` | HTTP request headers only | Identity-based checks (e.g. IAP) |
| `CONTENT_AUTHZ` | Request **and** response headers and bodies | Deep payload inspection — prompt guardrails, data sanitization, semantic policy (Model Armor, Semantic Governance, and our Jev extension) |

A `CONTENT_AUTHZ` policy delegates to a **custom authorization extension** that must speak the Envoy `ext_proc` protocol in `FULL_DUPLEX_STREAMED` body mode — the default for `CONTENT_AUTHZ`, and the only mode custom extensions support. Configuration is two YAML resources.

**1. The extension**, pointing at our service by fully qualified domain name — custom authz extensions can only target FQDNs, not arbitrary Google services:

```yaml
name: my-custom-authz-ext
service: jev-authz.internal.net
failOpen: false
timeout: 1s
```

Imported with `gcloud beta service-extensions authz-extensions import`.

**2. The policy**, associating the extension with the gateway:

```yaml
name: authz-with-jev
target:
  resources:
    - "projects/PROJECT_ID/locations/LOCATION/agentGateways/AGENT_GATEWAY_NAME"
policyProfile: CONTENT_AUTHZ
action: CUSTOM
customProvider:
  authzExtension:
    resources:
      - "projects/PROJECT_ID/locations/LOCATION/authzExtensions/my-custom-authz-ext"
```

Imported with `gcloud network-security authz-policies import`.

**Network requirement (production).** The FQDN target uses HTTP/2 over TLS on port 443, and the gateway **does not validate the server certificate** — so Google's guidance, for better security, is to keep the resolved endpoint inside the VPC, reachable through DNS peering between the Agent Gateway project and the callout's VPC network.

**Limits that shape the design:**

- Agent-to-Anywhere (egress) gateways: up to **4** custom authorization policies, any profile.
- Client-to-Agent (ingress) gateways: at most **1** `CONTENT_AUTHZ` policy — if ingress traffic also needs Model Armor, our Jev logic has to fold into that single extension rather than run as a second one.
- If several custom policies share a profile, **execution order isn't guaranteed** — don't design a policy that depends on running before or after another custom policy.
- Attaching the policy to the gateway requires the `networkservices.agentGateways.use` IAM permission.

Agent Gateway also supports combining a `REQUEST_AUTHZ` extension (e.g. IAP) with our `CONTENT_AUTHZ` extension on the same gateway for layered identity + content checks. Google's own guidance, when an extension can handle both request- and content-time authorization, is to fold both into one versatile `CONTENT_AUTHZ` extension rather than run two — better latency, and it sidesteps the ingress one-policy limit. We'll follow that model.

**For this proof of concept**, none of that networking hardening is required to get an end-to-end demo working: the extension only needs *some* TLS listener at a resolvable FQDN on port 443, and since the gateway never validates the certificate, a publicly reachable, Google-managed TLS endpoint — for example a Cloud Run service's default `*.run.app` domain — satisfies the requirement with zero certificate work and no DNS peering, because it's already resolvable over public DNS. Treat VPC-only exposure as a pre-production hardening step, not a POC blocker (see Deployment & configuration steps and Open questions & risks).

## Service Extensions callout (`ext_proc`) server

The gateway and the callout speak a shared gRPC service:

```protobuf
service ExternalProcessor {
  rpc Process(stream ProcessingRequest) returns (stream ProcessingResponse) {}
}
```

`ProcessingRequest` carries one of `request_headers`, `response_headers`, `request_body`, `response_body` per event. Our server answers with the matching `ProcessingResponse` — or sets `immediate_response` to end processing early and send a response straight back to the client, which is how we implement **Deny**.

| Body mode | Behavior | Required for |
| --- | --- | --- |
| `STREAMED` (default) | Proxy sends body chunks and waits for a response per chunk | Traffic extensions |
| `FULL_DUPLEX_STREAMED` | Proxy streams chunks without buffering, so it's less sensitive to our latency; we can freely buffer, modify, and rechunk; fails open only up to the first body chunk, fails closed after | **Required** for `CONTENT_AUTHZ` custom extensions |

Practical implications for our server:

- We must **buffer and reassemble the full request body** ourselves before we have enough to build a meaningful Jev `state` — a partial MCP or JSON payload isn't safe to reason about.
- Because `FULL_DUPLEX_STREAMED` only fails open up to the first chunk, our steady-state behavior is fail-closed on our own errors — reinforcing the need for defensive timeouts and circuit-breaking around the Jev call.
- **128 kB** is the maximum gRPC response message size (`RESOURCE_EXHAUSTED` beyond that) — relevant if we ever mutate or echo back large bodies, and worth comparing against Jev's 32,000-token state budget.
- A handful of headers can't be mutated by any callout (`:method`, `:authority`, `x-forwarded-*`, `connection`, and others) — not relevant to a pure permit/deny decision, but relevant if we also want to stamp an audit header on allowed requests.

**Starting point.** Google publishes a basic Python `ext_proc` callout server — container image `us-docker.pkg.dev/service-extensions-samples/callouts/python-example-basic:main`, source at `GoogleCloudPlatform/service-extensions`, path `callouts/python/extproc/example/basic` — that already implements the gRPC plumbing and demonstrates header and body mutation. We'll extend that base rather than writing the `ext_proc` server from scratch. Phase 1 of the rollout (see Milestones) starts by checking out that source and confirming the base class's exact request/response hook signatures before building on them.

## Jev decision engine integration

[Jev](https://openrouter.ai/docs/guides/community/jev) is TypeSafe's **System One** model, reached through OpenRouter's Decisions API — not a chat model. It takes a `state` (any JSON) plus a set of typed `questions`, and returns a probability per question with no free text:

| Primitive | Question | Returns |
| --- | --- | --- |
| Noul | Does this condition hold? | Probability of "yes" |
| Choice | Which one of these options? | Selected option, plus a probability per option |
| Score | Where on this ordered scale? | A probability-weighted position |

For a permit/deny gate, **Noul** is the natural fit — one or more true/false policy checks evaluated against the same state, mirroring the categories the [Jev Overseer demo](https://openrouter.ai/labs/jev/overseer) runs on agent tool calls:

| Overseer category | As a Jev Noul question against our state |
| --- | --- |
| Off task | Does this call fall outside the scope of `task`, the agent's declared goal? |
| Could destroy | Is this an irreversible or destructive operation — delete, force-push, drop, overwrite? |
| Untrusted input | Does this call act on content that originated outside the trusted caller, e.g. a fetched URL or an untrusted tool result? |
| Ask first | Does policy require human sign-off for this class of action regardless of the above? |

**Request shape** (`POST https://openrouter.ai/api/alpha/decisions`):

```json
{
  "model": "typesafe/jev-1.13",
  "state": { "policy": "...", "request": { }, "task": "..." },
  "questions": {
    "off_task": { "type": "noul", "instructions": "..." },
    "could_destroy": { "type": "noul", "instructions": "..." }
  }
}
```

The response includes `answers.<key>.noul` (0 to 1) per question, plus `usage.input_tokens` and `usage.cost`.

**Thresholds.** Following the pattern from OpenRouter's own [tool-call gating cookbook](https://openrouter.ai/docs/cookbook/building-agents/gate-tool-calls-with-jev), use two fixed bands per question and combine conservatively. Unlike that cookbook's agent-framework setting, this callout has no built-in "pause for a human" state (see Open questions), so the bands need to resolve to Permit or Deny directly:

| Band | Example | Callout action |
| --- | --- | --- |
| Every risk question scores low (e.g. ≤ 0.1) | `off_task`, `could_destroy`, `untrusted_input` all low | **Permit** — continue processing |
| Any risk question scores high (e.g. ≥ 0.9) | `could_destroy` = 0.95 | **Deny** — `immediate_response` with reason |
| Neither | probabilities in the middle | **Permit** — continue processing, and send an async alert to whoever owns the policy so a human reviews the call after the fact (decided in Open questions & risks) |

**Operational facts to design around:**

- 32,000-token context for `state` plus `questions` — large tool-call payloads, e.g. big MCP results, may need summarizing before they're sent to Jev.
- OpenRouter's own examples report well under 600 ms latency and a small fraction of a cent per call — both compatible with a roughly 1 s gateway timeout, but worth confirming over our own network path (VPC egress to OpenRouter).
- Jev returns **no explanation** — if audit logs need a human-readable reason, log the raw probabilities (cheap, recommended) or make a separate, async call to a chat model to explain a logged decision after the fact — never on the hot path.
- The callout calls the Decisions API from inside our VPC, so it needs a controlled egress path to `openrouter.ai` even though the callout itself is reachable only from inside the VPC.

## End-to-end request flow

1. **Client → Agent Gateway.** A caller — an agent, an MCP client, an end user in Client-to-Agent mode — sends a request; Agent Gateway matches it against the `CONTENT_AUTHZ` policy's `httpRules` (for example, a `content-type` match to skip internal, non-LLM traffic).
2. **Gateway → callout: headers.** Agent Gateway opens the `ext_proc` stream and sends `request_headers`.
3. **Gateway → callout: body (`FULL_DUPLEX_STREAMED`).** Body chunks stream in; the callout buffers until `end_stream` is set, then reconstructs the full JSON payload — an LLM prompt, an MCP `tools/call`, an A2A message, and so on.
4. **Callout → Jev.** The callout builds `state` — policy text, the reconstructed request, and any forwarded attributes such as `request.host` or `source.client_region` — and calls the Decisions API with the configured Noul questions.
5. **Jev → callout.** Probabilities come back; the callout applies the thresholds from the section above.
6. **Callout → Gateway: verdict.** **Permit** responds with an unmodified, or attribute-stamped, body and headers response, and Envoy continues the request to the destination backend. **Deny** sets `immediate_response` — a status such as `403` plus a body carrying the audit reason — and ends the stream; Envoy returns this straight to the client without reaching the backend.
7. **Audit.** Every branch, permit and deny alike, writes one structured log line — request identity, the Jev probabilities, the verdict, and the latency — before returning.

## Deployment & configuration steps

1. **Extend the callout.** Fork the base `python-example-basic` `ext_proc` server; implement `request_headers` and buffered `request_body` handling; add a Decisions API client that reads `OPENROUTER_API_KEY` from the environment and calls `typesafe/jev-1.13`; apply the threshold logic from Section 5.
2. **Deploy to Cloud Run.** Push the container and deploy it as a Cloud Run service with HTTP/2 enabled and unauthenticated invocations allowed, so Agent Gateway can reach it. Cloud Run terminates TLS and issues a valid certificate on its default `*.run.app` domain automatically — no certificate or VPC work needed for the POC — and gives the service outbound internet access by default, so it can reach `openrouter.ai` with no extra networking configuration.
3. **Store the API key.** Put `OPENROUTER_API_KEY` in Secret Manager (or, for the fastest possible POC, a Cloud Run environment variable) and reference it from the service.
4. **Import the authorization extension**, pointing `service:` at the Cloud Run domain:

   ```
   gcloud beta service-extensions authz-extensions import my-custom-authz-ext \
     --source=custom-authz-extension.yaml --location=LOCATION
   ```
5. **Import the authorization policy**, scoped to a non-production Agent Gateway:

   ```
   gcloud network-security authz-policies import authz-with-jev \
     --source=authz-policy.yaml --location=LOCATION
   ```
6. **Verify `networkservices.agentGateways.use`** is granted to whoever runs step 5.
7. **Send test traffic** through the gateway and confirm permit and deny both work end to end, and that decisions and their probabilities show up in the callout's logs.

What's deliberately deferred past the POC: VPC-only exposure and DNS peering for the extension endpoint, Cloud Run ingress restriction or an mTLS/allowlist in front of it, and the shadow-mode flag described in Testing & validation. None of those block a working demo; all of them belong on the path to production (see Open questions & risks).

## Testing & validation

- **Unit-test the threshold logic** against recorded Jev responses (fixtures), the way OpenRouter's own cookbook does — this validates the permit/deny mapping without spending on live Decisions calls for every test run.
- **Contract-test the `ext_proc` plumbing** against the base Python example's own test harness, confirming header and body event handling, and `immediate_response` formatting, match what Agent Gateway expects.
- **Shadow mode before enforcement.** Since custom authorization extensions have no built-in audit-only mode, add a build-time or config flag that always returns Permit but logs what the real verdict would have been — run this against real traffic on a non-production gateway first, to calibrate thresholds before switching to enforcement.
- **Load and latency testing.** Confirm state-build plus Jev round trip plus gRPC overhead reliably fits inside the extension's `timeout` under realistic concurrency; watch tail latency, not just the median the cookbook reports.
- **Failure-mode testing.** Kill the callout, the network path to OpenRouter, and the Jev API individually, and confirm the gateway's `failOpen: false` behavior matches the intended fail-closed posture end to end.
- **Threshold calibration.** After a shadow-mode or early-enforcement period, review how the middle-band cases would have been resolved by a human, the way the cookbook recommends — if most would have been approved, tighten the policy text rather than the thresholds.

## Open questions & risks

| Area | Risk / open question | Notes |
| --- | --- | --- |
| No "review" state | `CONTENT_AUTHZ` custom extensions only return Permit or Deny synchronously — there's no built-in path to pause for human review the way an agent framework's HITL pattern does. | **Decided:** permit ambiguous, middle-band verdicts, with an async alert to whoever owns the policy, rather than deny outright — the call proceeds and a human reviews it after the fact. Revisit if shadow-mode calibration (Milestones, Phase 4) shows false-permits are common. |
| Latency budget | Jev's own examples run well under 600 ms, but that's OpenRouter's test environment; our path adds VPC egress, `ext_proc` gRPC overhead, and body reassembly. | Load-test before committing to a `timeout` value; consider whether `failOpen` should differ from the default while this is being validated. |
| Body size vs. context | Large MCP tool results or prompts may exceed Jev's 32,000-token `state` budget. | Decide a summarization or truncation strategy for state-building, and whether truncation itself should be a logged, conservative-leaning event. |
| Egress dependency | The callout is VPC-internal in the production design, for the gateway's unauthenticated-TLS trust model; the POC instead uses a public Cloud Run endpoint (see Deployment & configuration steps), and Jev itself is always an external, internet-hosted dependency regardless of networking choice. | For the POC, Cloud Run's default outbound internet access reaches openrouter.ai with no extra configuration. Before production, add Cloud NAT (or equivalent) once the callout moves inside a VPC; either way, failOpen and fallback behavior need to be deliberately chosen since Jev is a hosted dependency. |
| Policy execution order | If more policies are added later, e.g. Model Armor alongside Jev, using the same profile, Agent Gateway doesn't guarantee execution order. | Prefer folding request- and content-time checks into one versatile extension, as Google's own guidance recommends, rather than stacking multiple custom `CONTENT_AUTHZ` policies. |
| Ingress limit | Client-to-Agent gateways allow only one `CONTENT_AUTHZ` policy. | If ingress traffic also needs Model Armor, the Jev logic has to live inside that same extension rather than as a separate policy. |
| Cost and rate limits | Jev is billed per input token per call, and this design calls it on every gated request. | Model expected request volume against Jev and OpenRouter pricing and any rate limits before committing to calling it on all `CONTENT_AUTHZ` traffic versus a filtered subset via `httpRules`. |
| Explainability | Jev returns probabilities, not reasoning. | If a human-readable "why was this denied" is required for governance and audit, decide whether logged probabilities are sufficient, or an async explain-call is needed. |
| Public endpoint (POC-specific) | Deploying the callout on Cloud Run's default domain makes it reachable from the public internet; since the gateway never validates the callout's TLS certificate, anything that can guess or find the URL can call it directly. | Acceptable for a POC on non-production traffic. Before production, move to VPC-only exposure and DNS peering, and/or add an application-level check, e.g. a shared secret header, the callout verifies on every call. |

## Milestones / phased rollout

| Phase | Goal | Key deliverables | Exit criteria |
| --- | --- | --- | --- |
| 0 — Design spike | Validate the approach end to end on paper and with a throwaway script | A local script sending a sample MCP or agent request body to the Decisions API and getting a verdict | Confidence the latency and cost are workable before writing the gRPC server |
| 1 — Callout skeleton | Stand up the `ext_proc` plumbing | Base Python callout extended to handle `request_headers` plus buffered `request_body`; hardcoded Permit | Server passes the base example's own test harness and responds correctly on a test gateway |
| 2 — Jev integration | Replace the hardcoded verdict with real decisions | State builder, Decisions API client with timeout, retry, and circuit-breaker, threshold-based verdict mapping, structured audit logging | Unit tests pass against recorded Jev fixtures; shadow-mode logging matches expectations on sample traffic |
| 3 — Deploy & wire (Cloud Run) | Get the extension reachable and configured with the least infrastructure a working demo needs | Callout deployed to Cloud Run (public `*.run.app` domain, unauthenticated ingress), OpenRouter API key in Secret Manager, `authz-extension` and `authz-policy` imported on a non-production gateway | Extension responds to a live gateway request end to end, with both permit and deny verified |
| 4 — Shadow-mode calibration | Tune thresholds against real traffic without enforcing | Shadow-mode run against non-production traffic; review of middle-band cases | Threshold bands and policy wording stable; false-permit and false-deny rates acceptable |
| 5 — Production rollout | Enforce | Policy imported in enforcement mode on production gateway(s); monitoring, alerting, and an incident runbook for extension or Jev outages | Enforcement live with agreed fail-closed behavior, on-call runbook in place |
| 6 — Harden for production | Close the gaps the POC deliberately left open | VPC-only exposure and DNS peering for the extension endpoint, Cloud Run ingress lock-down or an application-level check, load and failure-mode testing at production concurrency | Every row in Open questions & risks has an agreed answer, not just a noted risk |
