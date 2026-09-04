# Autonomy hot tuning production closeout

Date: 2026-09-04

## Status

Persistent per-chat autonomy hot tuning is merged and production-verified.

Production baseline:

```text
idle=3m
interval=5m
daily=24
drive=0.25
spontaneous=unlimited
```

The following explicit local controls are available:

```text
/autonomy tune
/autonomy tune idle 5m
/autonomy tune interval 10m
/autonomy tune daily 18
/autonomy tune drive 0.20
/autonomy tune spontaneous unlimited
/autonomy tune spontaneous 30
/autonomy tune reset
```

A successful tuning command persists the per-chat override in `runtime-preferences.sqlite` and affects subsequent autonomy/spontaneity decisions without restarting the process.

## Verified production behavior

Production verification confirmed:

- `/autonomy tune` reports the baseline as `3m / 5m / 24 / 0.25 / unlimited` with `customized=off`;
- changing `idle` and `drive` takes effect immediately without a service restart and reports `customized=on`;
- `/autonomy tune reset` restores the production baseline and returns `customized=off`;
- tuning is a deterministic local control path and does not depend on the selected Character LLM provider.

## Boundaries that remain code-owned

Hot tuning intentionally does not expose every autonomy guard. The following remain fixed safety/policy boundaries unless changed through normal code review:

```text
base proactive cooldown = 30m
max consecutive unanswered = 3
sleep-session cap = 2
salient-signal requirement = required
model/action validation = required
SILENT = always valid
process-level autonomy/spontaneity gates = not hot-tunable
```

Persona Core, Structured Memory, Character State, canonical transcript and provider selection are not modified by tuning.

## Development / validation rule

For a worktree that does not change `pyproject.toml` or dependency declarations, use the shared Server Dev venv read-only:

```bash
bash scripts/validate-dev.sh \
  --venv /opt/amadeus-bot-dev/.venv
```

This keeps the source checkout isolated through the worktree while avoiding repeated PyPI access on the server. The validator verifies import origin before running the suite.

If dependencies change, use a task-local `.venv` and perform a real install. Do not weaken TLS verification or mutate the shared venv to work around package-index failures.
