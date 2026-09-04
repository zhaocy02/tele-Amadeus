# 37. CHAR — Canon Persona Corpus

Status: **IMPLEMENTED / historical design record**  
Track: **CHAR**  
Updated: **2026-09-04**  
Current architecture: `docs/38-char-canon-three-track-architecture.md`  
Production closeout: `docs/53-persona-canon-phase1-closeout.md`

## 1. Original purpose

This document introduced the idea of using STEINS;GATE / STEINS;GATE 0 dialogue as **behavioral evidence** without flattening it into User Memory or a monolithic roleplay prompt.

The core design was:

```text
canon source files
      v
offline corpus builder
      v
read-only Canon Persona Corpus
      v
small runtime retriever
      v
existing Character Context Builder
```

That design is now implemented and running in production.

## 2. Final corpus result

```text
raw total             1367
  sg / kurisu          758
  sg0 / kurisu         195
  sg0 / amadeus        414

enriched kept          922
  sg / kurisu          528
  sg0 / kurisu         124
  sg0 / amadeus        270
```

Production Phase 1 corpus SHA-256:

```text
289ce7b0100fd3e3892950145614c3f39321bdfb04f1afe2e0ce94aa75c9cbba
```

The commercial/raw VN corpus remains outside normal Git history. Repository code owns extraction/build/enrichment/evaluation tooling and small synthetic fixtures, not proprietary game scripts.

## 3. Source model

The minimum source distinction remains:

```text
source = sg | sg0
persona = kurisu | amadeus
```

The final interpretation is stricter than the early v1 design:

```text
SG Kurisu + SG0 Kurisu
  -> ordinary personality / science / social baseline

SG0 Amadeus
  -> narrow digital identity / memory discontinuity /
     restart / deletion / continuity / existence /
     digital-condition relationship delta
```

Do not flatten these into one undifferentiated personality source.

## 4. Corpus record contract

The runtime corpus remains a local JSONL behavior corpus with provenance and bounded scene context. Core fields include:

```text
id
source
persona
history
response
act
tags
search_summary
```

`search_summary` is especially useful because runtime input is often Chinese while source dialogue may be English/Japanese. Offline behavior summaries make lightweight retrieval language-compatible without an always-on embedding service.

## 5. Runtime retrieval

The original lightweight retriever evolved into source-aware Behavior RAG v2.

Current properties:

- local/dependency-free runtime retrieval;
- bounded lexical/concept matching;
- Kurisu-default source policy;
- narrow Amadeus-delta admission;
- zero-example retrieval remains valid;
- weak semantic evidence is not rescued only by reranking;
- explicit current shared-history verification hard-zeroes Canon;
- no per-turn annotation LLM;
- no vector DB/embedding service required.

## 6. Character Context boundary

Canon remains explicitly separate from Structured Memory:

```text
Canon Corpus answers:
  how did Kurisu/Amadeus behave in comparable source situations?

Structured Memory answers:
  what has this Amadeus actually learned/experienced with this user?
```

Generation must not claim that a source-story event happened with the current user merely because a Canon example was retrieved.

The first production smoke verified this boundary on an explicit false shared-history probe.

## 7. Persona Distill and Evaluation

The corpus is now used in the full three-track loop:

```text
Canon Corpus
   +-- Persona Distill -> reviewed Persona Core
   +-- Behavior RAG    -> per-turn behavior evidence
   `-- Evaluation      -> regression/activation evidence
```

Phase 1 completed:

```text
922 enriched examples
-> Persona Distill v2
-> 8 human-reviewed rules
-> focused Persona-only re-check
-> Persona Core kurisu-v2.1.0
-> paired-policy Canon OFF/ON PASS
-> Production Canon ON
```

Distillation remains offline and reviewable. It never automatically rewrites Persona Core.

## 8. Fine-tuning / embeddings remain later options

Current evidence does not justify them.

Consider embeddings only if correct evidence exists, source routing is already correct, bounded retrieval repeatedly misses, and the failure is truly `retrieval_miss`.

Consider fine-tuning only if correct Persona/Memory/State/Canon reaches the Generator and the Generator still repeatedly fails to express the intended behavior across enough real evidence.

## 9. Final decision

The preferred approach proposed by this document has succeeded:

```text
owned SG/SG0 extraction
+ small local enriched behavior corpus
+ source-aware bounded retrieval
+ explicit Canon != User Memory boundary
+ offline Persona Distill
+ reusable evaluation
+ guarded production activation
```

The next work is production observation and calibration, not another corpus architecture rewrite. See `docs/53-persona-canon-phase1-closeout.md`.
