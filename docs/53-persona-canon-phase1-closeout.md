# 53. Persona / Canon Phase 1 production closeout

Status: **COMPLETE / production active**  
Date: **2026-09-04**  
Track: **CHAR / EVAL / OPS**  
Closes the execution loop tracked by Issue #49 and docs 38/40/44/47/52.

## 1. Final production state

The first complete SG / SG0 Persona + Canon loop is finished and running in production.

```text
Persona Core                 kurisu-v2.1.0
Canon raw corpus             1367
Canon enriched corpus         922
Canon corpus SHA-256         289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
Source-aware Canon retriever v2   PASS
Paired-policy Canon OFF/ON gate   PASS
Production Canon                  ON
```

The target remains:

```text
Kurisu personality / behavior baseline
+
Amadeus digital identity / memory / continuity / existence delta
+
current runtime Memory / Character State / real conversation history
```

Canon is behavior evidence only. It is not current-user memory and does not authorize source-story events as shared history.

## 2. Completed evidence chain

### Corpus and enrichment

```text
1367 raw examples
  sg / kurisu       758
  sg0 / kurisu      195
  sg0 / amadeus     414

922 retained enriched examples
  sg / kurisu       528
  sg0 / kurisu      124
  sg0 / amadeus     270
```

The retained corpus passed duplicate/provenance checks and carries bounded behavior metadata used by the runtime retriever.

### Persona Distill and promotion

```text
922 enriched examples
-> 48 extraction batches
-> 4 consolidation passes
-> 8 human-reviewed candidate rules
-> focused Persona-only re-check
-> explicit promotion review
-> Persona Core kurisu-v2.1.0
```

The distillation artifact never wrote Persona Core automatically. Promotion remained a reviewed, versioned Core change.

### Source-aware Behavior RAG v2

The final retriever keeps the implementation local and inspectable:

```text
ordinary personality / social / science
  -> Kurisu evidence by default

narrow digital identity / memory discontinuity /
restart / deletion / continuity / existence /
digital-condition relationship effects
  -> admit / prefer Amadeus-delta evidence
```

Important final properties:

- zero-example retrieval remains valid;
- current shared-history verification hard-zeroes Canon;
- weak semantic matches cannot be rescued only by source/act reranking;
- bounded concept/cue expansion is used before considering embeddings;
- no vector DB, embedding service, or per-turn annotation LLM is required.

### Evaluation

The first four-way real-data evaluation produced:

```text
16 fixed cases x 4 variants = 64 observations
```

The final activation gate used paired policy sampling so Canon OFF and ON shared the exact same Conversation Policy for each case:

```text
same Persona
same case
same recent conversation
same Conversation Policy
        |
        +-- Canon OFF
        `-- Canon ON
```

The final current-baseline paired run used the exact P6 head:

```text
f66fbc8fdcc4a6b4c66f7afc590c970c2eeefe9f
```

All 16 OFF/ON pairs matched policy fingerprint/act/mode/reason/obligation/intensity/state-bias, so behavior differences were no longer confounded by policy resampling.

Reviewed boundaries passed:

- evidence-based scientific revision;
- no-solution venting;
- disagreement / no fake agreement;
- Amadeus memory identity;
- restart / continuity;
- established-contact instrumentalization;
- ordinary zero-hit controls;
- novel no-close-exemplar control;
- explicit Canon/shared-history boundary;
- no source-scene quote/catchphrase copying or reenactment.

Local Canon retrieval/context overhead remained roughly 80-100 ms/turn, which does not justify an embedding/vector service.

## 3. Production activation

P7 added a guarded rollout path:

```bash
bash scripts/rollout-prod-feature.sh canon on
```

Activation fails closed unless the production Canon file exists, parses with the runtime loader, and contains examples. It prints count + SHA-256 before changing the gate.

The first production rollout used the exact P6 evidence corpus:

```text
source count       922
production count   922
source SHA-256     289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
production SHA-256 289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
CORPUS_MATCH        PASS
rollout             PASS
```

Production Canon was enabled on main containing rollout-helper merge:

```text
7ad65d258a3e03b4bbbc0e4e016d60a94224b248
```

Rollback remains:

```bash
bash scripts/rollout-prod-feature.sh canon off
```

## 4. Production smoke result

Focused Telegram smoke passed the intended behavior boundaries. In particular, the explicit false shared-history probe was rejected rather than converted into a remembered source-story event.

The first smoke also exposed an operations/testing boundary: ordinary production turns use normal persistence, so two synthetic probes produced Archivist records (`self_memory` and `open_thread`). Both records were explicitly marked `forgotten`, which also rotated the active conversation generation. Read-only verification showed no Character State history entries during the smoke window, so no Character State rollback was required.

This is not a Canon retriever regression. It is a production-smoke persistence issue and is tracked separately in Issue #103: **Add non-persistent production smoke mode**.

Final P7 decision:

```text
Production Canon              KEEP ON
Focused Telegram smoke        PASS
Shared-history boundary       PASS
Known smoke memory pollution  CLEANED
Character State contamination NONE
```

## 5. Original three-track architecture: implementation status

The original design was:

```text
                 SG / SG0 Corpus
                       |
          +------------+------------+
          |            |            |
          v            v            v
   Persona Distill   Behavior RAG   Evaluation
          |            |            |
          v            v            v
    Persona Core   per-turn cases  regression
          |            |            |
          +------+- ----+            |
                 v                  |
          Character Context <-------+
                 |
                 v
          Character Generator
                 ^
          +------+------+
          |             |
   Character State   User Memory
```

Phase 1 status now maps to it as follows:

| Architecture leg | Current implementation | Status |
| --- | --- | --- |
| SG / SG0 Corpus | owned/pinned extraction, raw preflight, enrichment, 922-example production corpus | **DONE** |
| Persona Distill | offline source-aware distillation + human review | **DONE** |
| Persona Core | reviewed promotion to `kurisu-v2.1.0` | **DONE / production** |
| Behavior RAG | source-aware local CanonRetriever v2 + bounded cue expansion + zero-hit behavior | **DONE / production ON** |
| Evaluation | fixed regression cases, four-way evaluation, paired-policy OFF/ON harness, exact-head evidence | **DONE / reusable** |
| Character Context | explicit Canon behavior section separated from Memory/State/runtime facts | **DONE / production** |
| Character Generator | consumes Persona + Policy + Memory + State + optional Canon/Web/Vision evidence | **DONE / production** |
| Character State | persistent subjective state + bounded retrospective path | **DONE / production** |
| User Memory | structured memory + provenance/correction + canonical transcript continuity | **DONE / production** |

Therefore the original architecture is no longer mainly a roadmap. **All major boxes in the three-track Character architecture now exist and are connected in the production runtime.**

## 6. What remains after Phase 1

The next work is not to add a missing core box. It is to improve operational quality and use real production evidence.

Current follow-ups:

1. **Issue #103 — non-persistent production smoke mode.** Production behavior tests should exercise real Persona + Canon + provider + Telegram paths without mutating normal transcript/memory/state.
2. **Production observation -> regression.** Confirmed real failures should become fixed cases with subsystem attribution rather than chat-only notes.
3. **Archivist negative-claim behavior.** A denied/fabricated shared-history probe should generally not become an `open_thread` unless the user actually establishes a real unresolved topic.
4. **Web Search latency attribution/optimization.** Preserve provenance while reducing user-visible delay.
5. **Provider resilience.** Automatic failover remains later work and must be bounded/observable; selectable providers already exist.

Still not justified by current evidence:

```text
vector DB
embedding service
per-turn Canon annotation LLM
fine-tuning
large behavior ontology
complex provider/orchestration framework
```

## 7. Maintenance rule from here

Treat `kurisu-v2.1.0` + the 922-example Canon corpus as the current production baseline.

Do not casually re-run distillation or retune retrieval because a single response feels different. For any material regression:

```text
observe
-> classify failure
-> reproduce with a focused case
-> change the responsible layer
-> exact-head validation/evaluation as required
-> guarded rollout
```

The Character architecture is now sufficiently complete that future improvements should usually be incremental calibration rather than another foundational rewrite.
