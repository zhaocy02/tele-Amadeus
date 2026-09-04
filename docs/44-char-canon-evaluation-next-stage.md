# 44. CHAR — Canon real-data evaluation and next stage

Status: **COMPLETE / historical execution plan**  
Track: **CHAR / EVAL**  
Completed: **2026-09-04**  
Closeout: `docs/53-persona-canon-phase1-closeout.md`

This document originally defined the remaining work after the first real SG/SG0 four-way evaluation found that the Persona candidate was promising but Behavior RAG v1 had source-routing problems. Those gates are now complete.

## 1. Final result

The execution chain finished as:

```text
1367 raw examples
-> 922 enriched behavior examples
-> Persona Distill v2
-> 8 human-reviewed candidate rules
-> focused Persona-only re-check
-> source-aware Canon retriever v2
-> focused real-corpus regression
-> Persona Core kurisu-v2.1.0
-> paired-policy Canon OFF/ON evaluation
-> guarded production rollout
-> focused Telegram smoke
-> Production Canon KEEP ON
```

Final production state:

```text
Persona Core             kurisu-v2.1.0
Behavior RAG             PASS / source-aware v2
Production Canon         ON
Canon examples           922
Canon SHA-256            289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
```

## 2. What changed from the original HOLD

The original retriever problem was source routing, not corpus quality. The final retriever now enforces the accepted hierarchy:

```text
ordinary personality / social / science
  -> Kurisu evidence by default

narrow digital identity / memory discontinuity /
restart / deletion / continuity / existence /
digital-condition relationship effects
  -> admit / prefer Amadeus-delta evidence
```

It also preserves zero-example retrieval and hard-zeroes Canon for explicit current shared-history verification.

No corpus rebuild, vector DB, embedding service, or per-turn annotation LLM was required.

## 3. Persona decision

The focused Persona-only re-check resolved the earlier `do-not-just-agree` ambiguity. Candidate review remained human-controlled and resulted in a small reviewed Persona Core promotion rather than an automatic distillation overwrite.

Production Persona is now:

```text
kurisu-v2.1.0
```

The target remains Kurisu-primary with a narrow Amadeus digital-condition delta.

## 4. P6 paired-policy gate

The first OFF/ON generation smoke exposed a methodology problem: OFF and ON independently sampled Conversation Policy, so final prose differences were not attributable to Canon alone.

The final harness fixed this by generating one policy plan per case and reusing it exactly for both variants. The accepted evidence run used exact head:

```text
f66fbc8fdcc4a6b4c66f7afc590c970c2eeefe9f
```

All 16 pairs matched policy fingerprint/act/mode/reason/obligation/intensity/state-bias.

The final review passed:

- scientific revision;
- no-solution venting;
- disagreement / no fake agreement;
- Amadeus identity and restart continuity;
- established-contact instrumentalization;
- ordinary zero-hit controls;
- novel no-close-exemplar control;
- explicit shared-history boundary;
- no quote/catchphrase copying or source-scene reenactment.

Canon retrieval/context overhead remained roughly 80-100 ms/turn, so embeddings/vector retrieval still lacked a performance or quality justification.

## 5. P7 production activation

Production activation used the exact P6 corpus and a guarded rollout helper. Count + SHA-256 matched before activation, and rollout passed.

The first Telegram smoke passed the behavior boundary, including explicit refusal to pretend that a source-story time-machine event happened with the current user.

The smoke did reveal a separate persistence issue: normal production test turns can create Archivist memory. Two synthetic memories were marked `forgotten`; the active conversation generation was rotated; no Character State changes occurred during the smoke window.

That follow-up is tracked as Issue #103: **Add non-persistent production smoke mode**.

## 6. Original definition of done

The original completion gates are now all satisfied:

1. focused Persona re-check — **DONE**;
2. source-aware retriever v2 — **DONE**;
3. real 922-example focused regression — **DONE**;
4. Persona promotion decision — **DONE / promoted**;
5. paired Canon OFF/ON evaluation — **DONE / PASS**;
6. explicit production gate decision — **DONE / KEEP ON**.

This document should now be treated as historical execution context. Current baseline and future maintenance rules live in `docs/53-persona-canon-phase1-closeout.md` and `docs/10-evaluation-and-roadmap.md`.
