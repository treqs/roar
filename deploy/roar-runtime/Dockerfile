# roar-runtime: hermetic runtime staging image for Kubernetes lineage capture.
#
# Two roles:
#   1. Init-container staging (default CMD): copies self-contained per-ABI
#      roar trees into a shared emptyDir so training pods need no network
#      installs at startup (the OpenTelemetry auto-instrumentation pattern).
#   2. Webhook server base: `python -m roar.backends.k8s.webhook` runs the
#      mutating admission injector (roar importable via the cp312 tree).
#
# Build context is the repo root and requires a packaged wheel in dist/
# (scripts/build_wheel_with_bins.sh). See scripts/build_runtime_image.sh.

FROM python:3.12-slim AS build

RUN pip install --no-cache-dir uv

COPY dist/ /tmp/dist/

# One self-contained dependency tree per supported CPython ABI. uv resolves
# each Python's wheels without needing that interpreter installed. The
# top-level sitecustomize.py mirrors roar_inject.pth: PYTHONPATH staging
# doesn't process .pth files, but Python imports `sitecustomize` from any
# sys.path entry — gated on ROAR_WRAP so it is inert outside instrumented
# process trees.
RUN set -e; \
    wheel="$(ls /tmp/dist/roar_cli-*.whl | head -1)"; \
    for version in 3.10 3.11 3.12 3.13; do \
      tag="cp$(echo "$version" | tr -d .)"; \
      uv pip install \
        --target "/opt/roar-runtime/$tag" \
        --python-version "$version" \
        --python-platform x86_64-unknown-linux-gnu \
        --link-mode=copy \
        "$wheel"; \
      printf '%s\n' \
        'import os' \
        'if os.environ.get("ROAR_WRAP") == "1":' \
        '    __import__("roar.execution.runtime.inject.sitecustomize")' \
        > "/opt/roar-runtime/$tag/sitecustomize.py"; \
    done

FROM python:3.12-slim

COPY --from=build /opt/roar-runtime /opt/roar-runtime

# Webhook-server role: make roar importable for this image's own python.
ENV PYTHONPATH=/opt/roar-runtime/cp312

# Staging role (default): copy the runtime trees into the shared mount.
CMD ["/bin/sh", "-c", "cp -a /opt/roar-runtime/. ${ROAR_RUNTIME_TARGET:-/roar-runtime}/"]
