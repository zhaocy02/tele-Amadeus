# Development, deployment and observation helpers

## `validate-dev.sh`

Use this helper for routine local and Server Dev validation instead of pasting the raw output from every tool.

### Isolated worktree, unchanged dependencies

For ordinary feature/fix/docs/config work that does **not** change `pyproject.toml`, the preferred exact-head Server Dev path is an isolated worktree plus the already-provisioned Dev virtualenv in read-only mode:

```bash
cd /opt/amadeus-worktrees/<work-item>

bash scripts/validate-dev.sh \
  --venv /opt/amadeus-bot-dev/.venv
```

This mode:

- does not install, upgrade, uninstall, or otherwise mutate the shared virtualenv;
- forces `PYTHONPATH` to the current checkout and verifies the actual `amadeus_bot` import origin;
- puts the selected virtualenv first on `PATH`, so subprocess shell helpers use the same compatible Python;
- refuses to run if `pyproject.toml` changed relative to `origin/main`;
- refuses a conflicting worktree-local `.venv`, preventing a partial/stale environment from shadowing the selected interpreter;
- runs Ruff, mypy, pytest, CLI/helper checks, and import validation from the exact worktree source;
- runs package build with `--no-isolation` only when the shared venv provides `setuptools.build_meta`; otherwise it prints an explicit `[SKIP] build` and the exact-head GitHub CI build remains mandatory.

A typical successful worktree run is:

```text
Amadeus Server Dev validation
repo=/opt/amadeus-worktrees/<work-item>
branch=DETACHED
head=<full-sha>
python=3.12.x
venv=/opt/amadeus-bot-dev/.venv
venv_mode=external-readonly
[PASS] import-path=/opt/amadeus-worktrees/<work-item>/amadeus_bot/__init__.py
[PASS] shell-helpers
[PASS] observation-cli
[PASS] vision-smoke-cli
[PASS] web-search-smoke-cli
[PASS] ruff
[PASS] mypy
[PASS] pytest
[SKIP] build (external venv lacks setuptools.build_meta; CI build remains required)
RESULT=PASS
summary=/tmp/amadeus-dev-validation-last.txt
```

Do not create a duplicate task-local `.venv` first and then fall back to `--venv`; shell helpers may prefer that partial environment. If a failed task-owned `.venv` exists, move or remove only that task-owned directory before using external-venv mode.

### Dependency-changing work

If the work item changes `pyproject.toml`, build-system requirements, or Python dependencies, use a task-local virtualenv and perform a real install:

```bash
cd /opt/amadeus-worktrees/<work-item>
python3 -m venv .venv
bash scripts/validate-dev.sh --install
```

`--install` first performs a quiet editable install of `.[dev]` into the checkout-local `.venv`. It cannot be combined with `--venv`.

If that install is blocked by PyPI/proxy/TLS/CA problems, classify it as an environment/install failure. Do **not** disable TLS verification or add insecure trusted-host/package-index exceptions just to make a duplicate worktree environment install. The dependency-changing Server Dev gate remains incomplete until a real install succeeds; GitHub CI still provides the remote package-build/install gate.

### Shared output behavior

When validation passes, the concise summary is copied to:

```text
/tmp/amadeus-dev-validation-last.txt
```

That summary is normally all that needs to be pasted into a review/chat.

If a step fails, the helper:

- marks that step `[FAIL]`;
- prints only the last 30 lines for the failing command;
- preserves the complete validation log directory under `/tmp`;
- exits non-zero.

Use `--keep-logs` if successful-run logs should also be retained.

The helper runs the normal repository gates plus shell/operator-CLI validation:

```text
bash -n scripts/*.sh
bash scripts/deploy-prod.sh --help
bash scripts/vision-provider-smoke.sh --help
python -m amadeus_bot.telemetry_cli --help
python -m amadeus_bot.vision_smoke --help
python -m amadeus_bot.web_search_smoke --help
ruff check .
mypy amadeus_bot
pytest
python -m build                 # repo-local venv mode
python -m build --no-isolation  # external venv only when backend is available
```

## `deploy-prod.sh`

Use this helper for routine **no-schema-change** production code deployments after a PR is merged to `main`.

Run it only from the independent SSH session used for production operations:

```bash
cd /opt/amadeus-bot
bash scripts/deploy-prod.sh
```

The helper deliberately refuses to operate unless all of these are true:

- its checkout resolves exactly to `/opt/amadeus-bot`;
- the checkout is on `main`;
- the Git working tree is clean;
- the production `.venv` and `.env` exist;
- local `main` can fast-forward to the fetched `origin/main`;
- legacy v1 is not active;
- Amadeus v2 and CPA are active before restart.

For a normal deploy it then:

1. fetches `origin`;
2. fast-forwards local `main` to the fetched `origin/main`;
3. verifies deployed `HEAD == origin/main`;
4. resolves a verified CA bundle for the production pip install;
5. quietly installs the checkout into its own production `.venv`;
6. validates production config by sourcing `.env` without printing secrets;
7. verifies v1/v2/CPA service states;
8. restarts only `amadeus-telegram-bot.service`;
9. waits briefly and verifies service states again;
10. prints a compact summary and asks for a final human Telegram smoke.

### pip / TLS certificate behavior

The deployment helper never disables TLS verification and never uses `--trusted-host`.

For the editable install it resolves the certificate source in this order:

```text
1. explicit PIP_CERT, if set and readable
2. readable host system CA bundle
   /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
   /etc/pki/tls/certs/ca-bundle.crt
   /etc/ssl/certs/ca-certificates.crt
3. pip's normal default certificate behavior if none of those files exists
```

An explicit but unreadable `PIP_CERT` is a fail-closed deployment error; the helper does not silently fall back around an operator-specified certificate path.

The selected non-secret source is shown in the concise summary, for example:

```text
pip_cert=system:/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
```

This behavior was added after a production deploy safely stopped before restart when pip's default Python trust chain could not validate PyPI, while the host system CA bundle validated the same endpoint successfully.

Example successful output:

```text
Amadeus production deploy
repo=/opt/amadeus-bot
branch=main
head_before=<old-sha>
[PASS] clean-worktree
[PASS] git-fetch
origin_main=<merged-main-sha>
[PASS] fast-forward-eligible
[PASS] git-fast-forward
head_after=<merged-main-sha>
[PASS] exact-main
pip_cert=system:/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
[PASS] install
[PASS] config
pre-restart services: v1=inactive v2=active cpa=active
[PASS] restart
post-restart services: v1=inactive v2=active cpa=active
RESULT=PASS
telegram_smoke=required
summary=/tmp/amadeus-prod-deploy-last.txt
```

Normally that short summary is all that needs to be pasted into a review/chat. Full command output is captured under `/tmp` and is kept automatically on failure. Use `--keep-logs` to retain logs after success as well.

Use:

```bash
bash scripts/deploy-prod.sh --skip-restart
```

only when deliberately updating/installing/checking production without restarting the active service. A normal merged-code deployment should use the default restart behavior.

This helper is **not** for schema/data migrations, production cutover, rollback, or feature-branch testing. Those remain explicit runbooks because their safety gates differ.

## Phase 5.4 observation commands

After Phase 5.4 is deployed, routine behavior review does not require `/status` after every chat turn.

From the production checkout:

```bash
cd /opt/amadeus-bot

amadeus-observe report --since 7d
amadeus-observe report --since 7d --routing --limit 30
amadeus-observe report --since 7d --slowest 20
```

The report reads `./data/v2/turn-telemetry.sqlite` and joins transcript IDs to `./data/v2/runtime.sqlite` only when a question preview is requested. The telemetry DB itself does not duplicate conversation prose.

When a real routing or memory problem is confirmed, attach a review label instead of relying on a chat note that will be forgotten later:

```bash
amadeus-observe review \
  --turn turn_abcd1234 \
  --routing false_fast \
  --note "shared-history cue should have stayed deliberate"

amadeus-observe review \
  --turn turn_abcd1234 \
  --memory retrieval_miss \
  --note "relevant memory exists but was absent from retrieved IDs"
```

Those labels are intended to become the evidence source for later regression tests and Phase 5.7 tuning.

## Phase 5.5 provider vision capability smoke

The current CPA/provider capability gate has already passed on Server Dev against `gpt-5.6-sol`:

```text
response=RED
VISION_CAPABILITY=PASS
```

The command remains useful as a repeatable capability diagnostic:

```bash
cd /opt/amadeus-bot-dev
bash scripts/vision-provider-smoke.sh
```

The helper deliberately:

- refuses the production checkout;
- uses the Server Dev checkout's `.venv`;
- reads the existing provider settings from `/opt/amadeus-bot/.env` without printing them;
- removes Telegram credentials and forces long polling off before the probe starts;
- does not open Telegram, read runtime SQLite, or modify production state.

The probe submits a tiny embedded red PNG through the Responses `input_image` form and asks for exactly `RED`.

Interpretation:

```text
VISION_CAPABILITY=PASS
  current provider path accepts and understands the known image

VISION_CAPABILITY=FAIL
  provider/transport rejected or could not complete the request

VISION_CAPABILITY=INCONCLUSIVE
  request succeeded but the model did not correctly identify the test image
```

Phase 5.5 is now production-verified; see `docs/31-phase5-5-telegram-photo-vision.md` and `docs/32-phase5-5-production-verification.md`.
