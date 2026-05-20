# Phalanx — project guidelines

Phalanx is a public portfolio project. README and docs are public-facing — they describe prior security and agentic work neutrally without naming former employers or products.

## Commit conventions

Conventional commits. Allowed types: `feat`, `fix`, `refactor`, `docs`, `test`, `perf`, `chore`, `security`.

Allowed scopes: `agents`, `guardrails`, `tools`, `cli`, `audit`, `target`, `infra`, `docs`.

Example: `feat(agents): add planner with Pydantic-validated output schema`

## Commit workflow

- Always branch off `main`. Never commit directly to `main`.
- Stage specific files by name. Never `git add -A` or `git add .`.
- Never commit `.env`, credentials, or any file under `out/`.
- After committing, push the branch and open a PR with a Summary and Test Plan. The commit task is not done until the PR exists.
- On Windows, prefix `git` commands with `sleep 2` to avoid `index.lock` collisions.
- No AI-attribution trailers in commit messages.

## Running everything in Docker

The host should not need Python, Node, or any toolchain other than Docker Desktop. Every command in this repo runs via `docker compose`:

```bash
docker compose up phalanx-run     # the studio against target/
docker compose run --rm tests     # pytest in the agent container
```

If you find yourself reaching for `pip install` or `python` on the host, fix the Docker setup instead.

## Architecture invariants

- Agent I/O is always Pydantic-validated. If validation fails, the run halts — never silently coerce.
- Side effects (filesystem writes, shell, network) go through `phalanx.guardrails`. No agent module imports `subprocess`, `os.system`, or `socket`.
- The audit log is append-only and is the only source of truth for reconstructing a run.
- The egress allowlist lives in `docker-compose.yml`. Adding a host requires a code change.

## What this repo is and is not

It is a reference implementation that makes architectural claims runnable. It is not a finished framework, not a SaaS product, and not a substitute for human code review on production deployments.
