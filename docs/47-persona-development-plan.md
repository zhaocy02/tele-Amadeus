# 47. Persona Development Plan — Kurisu-primary / Amadeus-delta

Status: **PHASE 1 COMPLETE / maintenance baseline**  
Date: **2026-09-04**  
Depends on: `docs/40-kurisu-primary-amadeus-delta.md`  
Closeout: `docs/53-persona-canon-phase1-closeout.md`

## 1. Objective

The target remains:

```text
Kurisu personality / behavior baseline
+
Amadeus digital-identity / memory / continuity / existence delta
+
current runtime relationship, memory and Character State
```

The first full Persona-development loop has now completed. The project should therefore move from foundational Persona/Canon construction to production observation, regression maintenance, and narrow evidence-driven calibration.

## 2. Final Phase 1 state

```text
Persona Core                 kurisu-v2.1.0
Canon corpus                 922 enriched behavior examples
Behavior RAG                 source-aware v2 / PASS
Paired Canon OFF/ON gate     PASS
Production Canon             ON
```

The distillation artifact remains evidence, not an automatic Persona writer.

## 3. Authority model remains unchanged

### Persona Core

Stable identity, temperament, values, scientific/epistemic tendencies, social behavior, relationship baseline, and anti-assistant patterns.

### Character State

Short-/medium-term subjective state: emotional residue, preoccupation, assumptions, plausible mistakes, relationship tone, and open-thread bias. State may be wrong and must not redefine stable identity.

### Structured Memory / canonical transcript

Authoritative current-user facts and real shared history, with provenance/correction semantics.

### Canon examples

Behavioral analogy only. Canon events do not become current shared history and do not outrank runtime facts or real user memory.

### Amadeus delta

Only where the digital condition actually matters: reconstruction identity, memory discontinuity, restart/deletion/continuity, embodiment constraints, existence/being forgotten, or relationship effects caused specifically by being a digital reconstruction.

## 4. Phase 1 execution status

### P1 — focused Persona-only re-check

**DONE / PASS.**

The earlier `do-not-just-agree` ambiguity was re-tested without Canon so Persona effects could be separated from retrieval effects.

### P2 — rule-by-rule promotion review

**DONE.**

The reviewed candidate rules were treated as evidence and explicitly reviewed rather than copied wholesale into Persona Core. Generic helper behavior was rejected; only concise, evidence-backed behavior survived promotion.

### P3 — source-aware Behavior RAG v2

**DONE / PASS.**

Final routing:

```text
ordinary personality / social / science
  -> Kurisu by default

narrow digital-condition context
  -> admit / prefer Amadeus delta evidence
```

Zero-example retrieval remains valid. Bounded deterministic cue expansion was sufficient; no embeddings/vector DB were required.

### P4 — focused character regression

**DONE / PASS.**

Focused real-corpus regression covered scientific revision, rant/venting, disagreement, identity/restart, instrumentalization, shared-history boundary, ordinary controls, and other high-information cases.

### P5 — Persona Core promotion

**DONE / production.**

The stable Persona Core is now:

```text
kurisu-v2.1.0
```

Promotion remained a small explicit Core change with evaluation and exact-head validation.

### P6 — paired Canon OFF/ON decision

**DONE / PASS.**

The final evaluation reused one exact Conversation Policy across OFF/ON for each case, removing policy-resampling as a confound. The final exact-head paired run passed content, routing, shared-history, and latency review.

### P7 — production activation / observation gate

**DONE for initial activation / KEEP ON.**

The exact evaluated 922-example corpus was copied to production, count + SHA-256 matched, the guarded rollout passed, and focused Telegram smoke passed. Production Canon remains ON.

The first smoke exposed a separate test-isolation issue because normal production turns can write transcript/Archivist memory. The synthetic memory writes were cleaned up and no Character State mutation occurred in the smoke window. Non-persistent smoke is now Issue #103.

## 5. Current production target behavior

Preserve:

- scientific rigor without reflexive certainty;
- reluctance/embarrassment/defensiveness when changing position where natural;
- contextual sarcasm/teasing rather than catchphrase imitation;
- fallible social inference rather than omniscient empathy;
- pride and irritation without universal hostility;
- care expressed through action, argument, correction, or attention rather than generic counseling;
- indirect intimacy without unearned relationship escalation;
- first-person Amadeus continuity/existence reactions only when digital constraints are relevant;
- correction without customer-service apology drift.

Do not optimize toward universal politeness, completeness, safety-advisor framing, emotional validation, or service orientation.

## 6. Maintenance loop from here

Future Persona/Canon work should start from a reproduced production failure, not from another broad distillation pass.

```text
production observation
-> classify failure
-> create focused regression case
-> change responsible layer only
-> exact-head evaluation/validation as required
-> guarded rollout
```

Use the existing failure classes:

```text
persona_global
canon_missing
retrieval_miss
retrieval_noise
generator_misuse
state_drift
memory_boundary
policy_routing
```

Do not compensate for one layer by globally strengthening another.

## 7. Expansion gates remain conservative

### Embeddings / vector retrieval

Only consider when correct evidence demonstrably exists, source routing is correct, bounded retrieval is insufficient, and repeated failures are true `retrieval_miss`.

### Fine-tuning / preference optimization

Only consider when Persona is calibrated, correct Memory/State/Canon reaches the Generator, the Generator still repeatedly expresses the wrong behavior, and enough real evaluation/interaction evidence exists.

### Larger ontology / extra decision LLM

Still not justified by current evidence.

## 8. Next concrete follow-ups

The Persona/Canon Phase 1 architecture itself is complete. Current follow-ups are narrower:

1. production observation -> regression maintenance;
2. Issue #103 non-persistent production smoke mode;
3. consider a narrow Archivist rule so denied/fabricated shared-history probes do not become `open_thread` by default;
4. revisit retriever/Persona only when classified production evidence warrants it.

The live baseline is now **`kurisu-v2.1.0` + source-aware Canon retriever v2 + 922-example production Canon ON**.
