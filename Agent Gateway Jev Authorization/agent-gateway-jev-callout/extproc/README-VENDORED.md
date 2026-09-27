# Vendored from GoogleCloudPlatform/service-extensions

`extproc/service/callout_server.py`, `callout_tools.py`, and
`command_line_tools.py` are copied, unmodified, from
[`GoogleCloudPlatform/service-extensions`](https://github.com/GoogleCloudPlatform/service-extensions),
path `callouts/python/extproc/service/`, under the Apache License 2.0 (see
`LICENSE` in this directory). This is the same base the "basic" Python
`ext_proc` example server uses
(`callouts/python/extproc/example/basic/service_callout_example.py`), which
the implementation plan (Section 4) calls out as the starting point.

They're vendored here rather than pulled in as a git submodule or a
separate pip install so the whole project builds with nothing beyond
`pip install -r requirements.txt` -- no `buf` CLI, no cloning a second
repo. The upstream project generates its own `envoy.*` protobuf/gRPC stubs
at build time with `buf generate` against `envoyproxy/envoy`; this project
uses the pre-built `xds-protos` package from PyPI instead, which ships the
same `envoy.service.ext_proc.v3.external_processor_pb2` module under the
same import path, so these three files needed zero changes to work against
it.

If you'd rather track upstream directly (e.g. to pick up a security fix or
a new hook), replace this directory's `service/` files with a fresh copy
from the upstream repo and re-run the tests in `tests/`.
