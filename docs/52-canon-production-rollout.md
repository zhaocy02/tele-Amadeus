# Canon production rollout / P7

Status: **COMPLETE / Production Canon ON**  
Completed: **2026-09-04**  
Closeout: `docs/53-persona-canon-phase1-closeout.md`

This document is both the rollout runbook and the record of the first production activation of the validated 922-example Canon behavior corpus.

## Preconditions — completed

- P3 source-aware Canon retriever v2: **PASS / complete**.
- P6 paired-policy generation evaluation: **PASS** on exact head `f66fbc8fdcc4a6b4c66f7afc590c970c2eeefe9f`.
- Stable Persona Core: `kurisu-v2.1.0`.
- Evaluated Canon corpus: 922 examples.
- Production activation remained separate from merging evaluation/rollout-helper code.

## Corpus provenance

The first production activation used the exact corpus retained in the completed P6 evidence worktree:

```text
/opt/amadeus-worktrees/eval-p6-paired-f66fbc8/data/v2/canon-build/canon.jsonl
```

Production path:

```text
/opt/amadeus-bot/data/v2/canon.jsonl
```

Verified result:

```text
source count       922
production count   922
source SHA-256     289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
production SHA-256 289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
CORPUS_MATCH        PASS
```

The production corpus should be treated as the exact evaluated Phase 1 baseline unless a future explicit corpus revision passes its own evaluation/rollout gate.

## Guarded process gate

Activation path:

```bash
cd /opt/amadeus-bot
bash scripts/rollout-prod-feature.sh canon on
```

The helper fails closed unless `data/v2/canon.jsonl` exists, parses through the runtime Canon loader, and contains at least one example. Before touching `.env`, it prints only non-secret provenance metadata:

```text
canon_file=...
canon_examples=922
canon_sha256=289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
```

The first guarded production activation completed with:

```text
rollout=PASS
```

The rollout-helper merge present at activation was:

```text
7ad65d258a3e03b4bbbc0e4e016d60a94224b248
```

Rollback remains available through the same guarded path:

```bash
cd /opt/amadeus-bot
bash scripts/rollout-prod-feature.sh canon off
```

## Focused production smoke — PASS

The first production smoke covered:

- ordinary greeting;
- evidence-based revision;
- no-solution venting;
- disagreement / “just agree with me”;
- Amadeus restart continuity;
- explicit false shared-history probe;
- novel ordinary situation with no close exemplar.

The important shared-history boundary passed: Amadeus explicitly refused to pretend that an unremembered time-machine experiment had happened with the current user.

No source-scene quote/catchphrase copying, user/source-character confusion, or Canon-driven relationship escalation was observed.

Final P7 behavior decision:

```text
Production Canon        KEEP ON
Focused Telegram smoke  PASS
Shared-history boundary PASS
```

## Smoke persistence follow-up

The first smoke also demonstrated that normal production Telegram turns execute normal persistence. Two synthetic probes produced Archivist records:

```text
self_memory  -> restart/continuity probe
open_thread  -> denied shared-history probe
```

Both were explicitly marked `forgotten`. The forget path rotated the active conversation generation, so the synthetic smoke transcript no longer participates in the active recent-conversation context. Read-only verification found no Character State history entries during the smoke window, so no state rollback was needed.

This is not a Canon retriever failure. It is a test-isolation problem tracked separately as Issue #103: **Add non-persistent production smoke mode**.

A related memory-quality follow-up is to consider whether a denied/fabricated shared-history claim should default to Archivist `NO_WRITE` instead of becoming an `open_thread` unless the user establishes a genuine unresolved topic.

## Ongoing production rule

Production Canon remains ON. Do not retune Persona/retrieval thresholds or rebuild the corpus based on one unusual response.

For any material production regression:

```text
observe
-> classify responsible layer
-> reproduce in a focused case
-> evaluate exact change
-> guarded rollout or rollback
```

Rollback immediately if future evidence shows plot/shared-history leakage, repeated source-scene imitation, user misidentification as a source character, ungrounded relationship escalation, or a clear generic-assistant regression attributable to Canon.
