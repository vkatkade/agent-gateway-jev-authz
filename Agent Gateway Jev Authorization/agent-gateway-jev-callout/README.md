# Agent Gateway custom authz callout, backed by Jev

A proof-of-concept `ext_proc` callout for Google Cloud Agent Gateway's
custom `CONTENT_AUTHZ` authorization extension. It inspects each request's
body, asks [Jev](https://openrouter.ai/docs/guides/community/jev)
(TypeSafe's System One decision model, via OpenRouter's Decisions API) a
small set of yes/no policy questions about it, and returns a Permit or Deny
verdict for Agent Gateway to enforce.

This implements the plan in *Agent Gateway Custom Authz via Jev —
Implementation Plan* (the design doc this project was built from). Section
numbers referenced below are that doc's.

## What's here

```
extproc/service/       Vendored, unmodified, from GoogleCloudPlatform/service-extensions
                        (Apache 2.0) -- the ext_proc gRPC plumbing. See
                        extproc/README-VENDORED.md for provenance.
jev_authz/
  state.py              Turns ext_proc headers + a reassembled request body
                         into the `state` object sent to Jev.
  jev_client.py          Minimal OpenRouter Decisions API client.
  policy.py              Loads policy.yaml; turns Jev's probabilities into a
                         Permit/Deny verdict (the three-band logic from
                         Section 5, as resolved in Section 9).
  server.py               The callout itself: buffers body chunks, calls the
                         above, returns Permit/Deny to Agent Gateway.
policy.yaml             The Jev questions and thresholds. Edit this, not the
                         Python, to change what's checked.
deploy/                 authz-extension and authz-policy YAML templates
                         (Section 3, Section 7).
Dockerfile              Cloud Run image (Section 7's POC path).
tests/                  Unit tests -- no network, no live Jev calls.
```

## How it works

1. Agent Gateway opens an `ext_proc` stream to this server for every
   request the `CONTENT_AUTHZ` policy matches, and sends the request
   headers, then the body in `FULL_DUPLEX_STREAMED` chunks.
2. **Every non-final body chunk is withheld**, not passed through. In
   full-duplex mode, chunks the callout releases start flowing to the
   destination backend immediately — if we let early chunks through and
   only decided Deny on the *last* chunk, some bytes could already be on
   their way to the backend. So nothing reaches the backend until the
   whole body is reassembled and a verdict is reached. See the docstring
   at the top of `jev_authz/server.py` for the details.
3. On the final chunk, the server builds `state` (policy text + the
   request + an optional declared task) and asks Jev the questions in
   `policy.yaml`.
4. Jev returns a probability per question. `policy.py` turns those into a
   verdict:
   - every question scores low (≤ `approve_max`) → **Permit**
   - any question scores high (≥ `deny_min`) → **Deny**, with a reasoned
     JSON body
   - otherwise (ambiguous) → **Permit**, with an alert flag for an async
     reviewer (the decision recorded in the plan doc's Open questions &
     risks)
5. On Permit, the *entire* reassembled body is released on that final
   chunk (not just the last piece — see point 2). On Deny, the server ends
   the stream with an `immediate_response` (403 + reason) that never
   reaches the destination backend.
6. Every decision — permit or deny — is logged as one structured JSON
   line, including the per-question probabilities and latency.

## Requirements

* Python 3.11+
* An [OpenRouter API key](https://openrouter.ai/settings/keys) with
  credits for `typesafe/jev-1.13`

No certificates, no VPC, no `buf` CLI — that's deliberate; see "What this
POC skips" below.

## Quickstart (local)

```shell
python -m venv env && source env/bin/activate
pip install -r requirements.txt

# Unit tests -- no network, no API key needed.
pytest tests/ -v

# Run the server locally in plaintext (no TLS) for manual testing.
export OPENROUTER_API_KEY=sk-or-...
python -m jev_authz.server --disable_tls --combined_health_check \
  --plaintext_address=0.0.0.0:8080
```

With the server running, drive it with any `ext_proc` client (for example
[`grpcurl`](https://github.com/fullstorydev/grpcurl) against
`envoy.service.ext_proc.v3.ExternalProcessor/Process`, or the sample
clients in the upstream `service-extensions` repo's `tests/` directories)
to send a `request_headers` event followed by one or more `request_body`
events, and confirm you get back either a body mutation carrying the full
body (Permit) or an `immediate_response` with status 403 (Deny). The real
caller in production is Agent Gateway itself, not a hand-written client —
this is just for local sanity-checking before you wire up the gateway.

## Configuring the policy

Edit `policy.yaml`. Each entry under `questions:` becomes one Jev Noul
check; `approve_max` and `deny_min` set the three bands described above.
No code change needed to add, remove, or reword a check. Point the server
at a different file with the `JEV_POLICY_PATH` environment variable.

## Deploying the POC (Cloud Run, Section 7)

No VPC, no DNS peering, no certificates: Cloud Run's default `*.run.app`
domain already has a valid, Google-managed TLS certificate, and Agent
Gateway's custom authz extension never validates the callout's
certificate anyway — so a public Cloud Run endpoint satisfies the
"HTTP/2 + TLS on port 443" requirement with zero extra work. See the plan
doc's Section 3 and Section 9 for why that's fine for a POC and what to
harden before production.

```shell
# 1. Build and deploy.
gcloud run deploy jev-authz-callout \
  --source=. \
  --region=REGION \
  --allow-unauthenticated \
  --use-http2 \
  --set-secrets=OPENROUTER_API_KEY=openrouter-api-key:latest
# (or --set-env-vars=OPENROUTER_API_KEY=sk-or-... for the fastest possible
# POC -- put it in Secret Manager before this becomes anything but a demo)

# Note the service's domain from the deploy output, e.g.
# jev-authz-callout-abc123-uc.a.run.app

# 2. Point the extension at it.
sed -i 's/SERVICE_DOMAIN/jev-authz-callout-abc123-uc.a.run.app/' deploy/custom-authz-extension.yaml
gcloud beta service-extensions authz-extensions import my-custom-authz-ext \
  --source=deploy/custom-authz-extension.yaml --location=LOCATION

# 3. Attach the policy to a non-production Agent Gateway.
sed -i 's/PROJECT_ID/your-project/; s/LOCATION/your-location/; s/AGENT_GATEWAY_NAME/your-gateway/' \
  deploy/authz-policy.yaml
gcloud network-security authz-policies import authz-with-jev \
  --source=deploy/authz-policy.yaml --location=LOCATION

# 4. Send test traffic through the gateway and check Cloud Run's logs for
#    the "jev_authz_decision" JSON lines -- one per request, permit and
#    deny alike.
```

`networkservices.agentGateways.use` is required to run step 3.

## What this POC skips (see the plan doc, Open questions & risks)

Deliberately deferred, not forgotten:

* **VPC-only exposure and DNS peering** for the callout endpoint — the
  public Cloud Run domain is reachable by anyone who finds the URL, since
  the gateway never validates its certificate. Fine for a POC on
  non-production traffic; add VPC-only exposure or an application-level
  check (e.g. a shared-secret header) before production.
* **Shadow mode** — there's no built-in dry-run for custom authz
  extensions (unlike IAP's `iamEnforcementMode: DRY_RUN`). Before
  enforcing on real traffic, consider adding a flag that always permits
  but logs what the verdict would have been.
* **Retries and a circuit breaker** around the Jev call — right now a
  single failed call denies (fail closed) and moves on; a sustained Jev
  outage will deny everything until it recovers.
* **Repeated-header fidelity** — `headers_to_dict` keeps only the last
  value of a repeated header.
* **Load and failure-mode testing** at production concurrency.

## License

`extproc/service/` is Apache 2.0, from Google; see
`extproc/README-VENDORED.md` and `extproc/LICENSE`. The rest of this
project (`jev_authz/`, `policy.yaml`, `deploy/`, `Dockerfile`, `tests/`)
has no license header attached — add whatever your organization uses
before this goes further than a POC.
