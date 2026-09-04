# tele-Amadeus

A Telegram-based persistent character runtime inspired by **Amadeus / Kurisu** from the STEINS;GATE series.

This repository is a **sanitized public downstream mirror** of the actively developed private upstream. It contains the reusable runtime, tests, public design documentation, synthetic evaluation assets, and generic deployment tooling while intentionally excluding private operational context, personal conversation data, credentials, runtime databases, and commercial VN corpus data.

## Project status

The current public baseline includes:

```text
Persistent Telegram Character Runtime
Structured Memory + canonical transcript
Character State + low-frequency Retrospective
Hybrid Conversation Policy
Persona Core: Kurisu-primary / Amadeus-delta
Source-aware Canon Behavior RAG
Vision-capable provider routing
Direct-turn Web Search with provenance
Long-horizon autonomy
Bounded short-horizon spontaneity episodes
CPA/Codex + DeepSeek provider registry
Production-oriented observability and deployment helpers
```

Short-horizon spontaneity is implemented as a bounded thought episode rather than an unbounded self-triggering loop. The default design uses a 1–30s first target gap, 3–15s later gaps, independent `SILENT | CONTINUE` decisions at each depth, user-input priority, and a hard maximum of seven follow-ups. Later depths become progressively less likely, so the cap is a safety boundary rather than a target message count.

The current Persona/Canon phase has completed its first production loop:

```text
1367 raw canon behavior records
-> 922 retained enriched records
-> source-aware Persona Distill
-> reviewed Persona Core kurisu-v2.1.0
-> source-aware CanonRetriever v2
-> paired Canon OFF/ON evaluation
-> production activation
```

The actual commercial game scripts and the derived 922-example production corpus are **not distributed in this repository**.

## Architecture

```text
Telegram text/photo/control
        |
        v
Authorization + Durable Inbox
        |
        v
Per-chat FIFO Router
        |
        v
Recent Conversation
  + Structured Memory
  + Character State
        |
        v
Hybrid Conversation Policy
        |
        v
Optional Tool Dispatcher
  + Web Search evidence
        |
        v
Character Context
  Persona Core
  + Policy
  + Memory
  + Character State
  + optional Canon behavior examples
  + optional Vision/Web evidence
        |
        v
Character Generator
        |
        v
Selected LLM provider/model
        |
        v
Telegram reply
        |
        +--> canonical transcript / telemetry
        +--> Memory Archivist
        +--> Character State update
        `--> low-frequency Retrospective
```

The main authority rule is that **Persona, current user memory, Character State, Canon behavior evidence, Web evidence, and provider selection are separate layers**. Canon examples are behavioral references, not current shared-history facts.

## Quick start

Requirements:

- Python 3.11+
- a Telegram bot token
- at least one configured LLM provider

Create an environment and install:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Edit `.env` with your own credentials and provider configuration, then run:

```bash
python -m amadeus_bot
```

Never commit `.env`, runtime databases, logs, credentials, or personal conversation data.

## Persona and Canon data

The repository contains the tooling for:

- building a local Canon corpus from legally obtained source material;
- offline enrichment and Persona distillation;
- source-aware Behavior RAG;
- fixed regression cases and paired OFF/ON evaluation.

It does **not** include proprietary STEINS;GATE / STEINS;GATE 0 scripts or the private production corpus.

The implemented target is:

```text
Kurisu stable personality / behavior
+
narrow Amadeus digital identity / memory / continuity / existence delta
+
current user relationship / memory / Character State
```

See `docs/38-char-canon-three-track-architecture.md` and `docs/53-persona-canon-phase1-closeout.md` for the current design and first completed evidence loop.

## Public mirror policy

This repository is generated from a private canonical development repository through an allow-listed export and safety scan. The public mirror intentionally has a separate Git history.

Public contributions can be reviewed here, but accepted changes are expected to be ported into the private upstream first and then returned through the next sanitized export. This avoids making the public mirror an accidental source of private operational state.

See `docs/54-public-mirror.md` for the mirror boundary.

## Development

Run the normal checks with:

```bash
ruff check .
mypy amadeus_bot
pytest
python -m build
```

The project favors small, inspectable components over a large agent framework. New infrastructure is added only when a measured failure justifies it.

## Important boundaries

Do not commit or publish:

- Telegram/API credentials;
- `.env` files other than `.env.example`;
- SQLite/runtime databases;
- logs/backups containing personal state;
- private conversation history or structured memory;
- commercial VN scripts or derived private Canon corpus files.

This is a fan-built technical project and is not affiliated with or endorsed by the STEINS;GATE rights holders.
