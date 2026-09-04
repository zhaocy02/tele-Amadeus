# 38. CHAR — Canon Three-Track Architecture

Status: **implemented production architecture**  
Track: **CHAR**  
Updated: **2026-09-04**  
Closeout: `docs/53-persona-canon-phase1-closeout.md`

## 1. Architecture

The shared STEINS;GATE / STEINS;GATE 0 corpus is used in three complementary ways:

```text
                 STEINS;GATE / SG0 Corpus
                           |
              +------------+------------+
              |            |            |
              v            v            v
       Persona Distill   Behavior RAG   Evaluation
              |            |            |
              v            v            v
        Persona Core    per-turn cases  regression
              |            |            |
              +------+- ----+            |
                     v                  |
             Character Context  <-------+
                     |
                     v
             Character Generator
                     ^
              +------+------+
              |             |
       Character State   User Memory
```

These are three uses of the same evidence, not three separate character engines.

Phase 1 has now implemented every major box in this diagram and connected them in production.

## 2. Current implementation status

```text
DONE  Canon corpus
      - owned/pinned SG + SG0 extraction
      - speaker/source provenance
      - 1367 raw examples
      - 922 retained enriched behavior examples

DONE  Persona Distill
      - source-aware offline distillation
      - 48 extraction batches + 4 consolidation passes
      - 8 human-reviewed candidate rules
      - no automatic Persona Core mutation

DONE  Persona Core promotion
      - focused Persona-only regression
      - explicit reviewed promotion
      - kurisu-v2.1.0 in production

DONE  Behavior RAG v2
      - local dependency-free CanonRetriever
      - Kurisu-default / narrow Amadeus-delta source policy
      - bounded cue expansion
      - zero-example retrieval remains valid
      - current shared-history verification hard-zeroes Canon
      - production gate ON

DONE  Evaluation
      - fixed versioned regression cases
      - first four-way 16 x 4 = 64 observations
      - focused real-corpus regression
      - paired-policy Canon OFF/ON harness
      - exact-head provider evidence

DONE  Character Context integration
      - Canon rendered as behavior evidence
      - explicit separation from Memory/State/runtime facts

DONE  Character Generator integration
      - consumes Persona + Policy + Memory + State + optional Canon/Web/Vision evidence

DONE  Character State / User Memory foundations
      - persistent Character State + retrospective
      - structured memory + canonical transcript + provenance/correction
```

Production baseline:

```text
Persona Core         kurisu-v2.1.0
Canon examples       922
Behavior RAG         source-aware v2
Production Canon     ON
```

## 3. Responsibility of each leg

### Persona Distill — global behavioral prior

Offline analysis asks what stable cross-scene behavioral rules recur. Its output is reviewable evidence. It never rewrites Persona Core automatically.

The successful Phase 1 path was:

```text
corpus
-> proposed distilled rules
-> human review
-> focused regression
-> explicit Persona Core diff
```

### Behavior RAG — local situational analogy

Runtime retrieval asks how Kurisu / Amadeus behaved in a comparable situation.

Current policy:

```text
ordinary personality / social / science
  -> Kurisu examples by default

narrow digital identity / memory discontinuity /
restart / deletion / continuity / existence /
digital-condition relationship effects
  -> admit / prefer Amadeus-delta examples
```

Zero examples is valid. Canon is behavioral analogy, not current factual authority.

### Evaluation — development feedback loop

Evaluation determines whether a Persona/retrieval/generator change actually improves character fidelity without damaging naturalness, factual boundedness, relationship continuity, or latency.

The reusable evaluation stack now includes fixed cases, source/provenance inspection, paired-policy OFF/ON comparison, exact-head validation, and production observation.

## 4. Authority boundary

The architecture succeeds only if these remain distinct:

```text
Persona Core
  stable character behavior

Character State
  subjective mutable current posture

User Memory / canonical transcript
  real current-user facts and shared history

Canon examples
  source-story behavior evidence only
```

A retrieved Canon scene cannot become current shared history simply because it influenced a reply.

## 5. Final source hierarchy

```text
SG Kurisu + SG0 Kurisu
        |
        v
Kurisu behavioral/personality baseline
        |
        +-- SG0 Amadeus evidence
                |
                v
        digital-condition delta
```

Amadeus evidence specializes identity, memory discontinuity, continuity/existence, embodiment where relevant, and relationship effects caused specifically by digital existence. It does not own ordinary helpfulness or general personality.

## 6. Architecture expansion gate

Phase 1 did not require:

- vector DB;
- embedding service;
- per-turn Canon annotation LLM;
- online Persona Distill;
- automatic Persona self-modification;
- fine-tuning;
- large behavior ontology.

Keep those out until a repeated, classified failure proves the lightweight design insufficient.

Embeddings are only a candidate when the correct evidence exists, source routing is already correct, bounded retrieval still repeatedly misses, and the failure is genuinely `retrieval_miss`.

Fine-tuning is only a candidate when correct Persona/Memory/State/Canon reaches the Generator and the Generator still repeatedly expresses the wrong behavior across enough evidence.

## 7. Maintenance sequence from here

The architecture is no longer waiting for Canon-4/5/6 implementation. Future work should use the completed loop:

```text
production observation
-> failure classification
-> focused regression
-> responsible-layer change
-> exact-head evaluation/validation
-> guarded rollout
```

The current follow-up discovered during P7 is Issue #103: non-persistent production smoke mode, so production behavior validation does not mutate normal transcript/memory/state.
