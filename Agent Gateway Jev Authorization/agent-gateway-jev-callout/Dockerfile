FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extproc/ ./extproc/
COPY jev_authz/ ./jev_authz/
COPY policy.yaml .

ENV PYTHONUNBUFFERED=1

# Cloud Run terminates TLS at its own edge and forwards plaintext HTTP/2
# (h2c) to the container on $PORT (default 8080) -- so this server runs
# with TLS disabled and no separate health-check port, which is why the
# POC needs no certificates (plan doc, Section 3 and Section 7). Harden
# this (VPC-only exposure, real TLS) before production.
CMD ["sh", "-c", "python -m jev_authz.server --disable_tls --combined_health_check --plaintext_address=0.0.0.0:${PORT:-8080}"]
