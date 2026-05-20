# Phalanx agent runtime.
# Single image used for both the studio and the test harness.
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# git is used by the implementer to apply diffs to the target tree.
# We deliberately do NOT install curl, wget, or netcat — the egress
# firewall is the primary control, but reducing the attack surface
# inside the container is good hygiene.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY phalanx ./phalanx
COPY tests ./tests
RUN pip install --no-cache-dir -e ".[dev]"

# Non-root user. The orchestrator does not need root, and the
# guardrails are easier to reason about when nothing in the
# container can write to /etc or /usr.
RUN useradd --create-home --shell /bin/bash phalanx \
 && chown -R phalanx:phalanx /app
USER phalanx

ENTRYPOINT ["python", "-m", "phalanx.cli"]
CMD ["--help"]
