# 48. Provider Resilience and Multi-Provider Plan

Status: **Stage 1 implemented and production-validated; resilience roadmap remains active**  
Date: **2026-09-04**

Production closeout/evidence record: `49-multi-provider-production-closeout.md`.

## 1. Objective

Amadeus must not depend on CPA or any single upstream provider as its only viable runtime path.

The first provider-resilience stage is implemented:

```text
primary/default LLM
  CPA -> Codex

selectable alternate LLM
  DeepSeek API

independent capability paths
  Web Search -> CPA hosted | DeepSeek hosted
  Vision     -> provider/model capability routing
```

CPA/Codex remains the primary production path. Multi-provider support is resilience and experimentation infrastructure, not a replacement project.

## 2. Architectural principle

Keep Character Runtime provider-neutral. Do not fork Character Runtime, Memory, Persona, Character State, Policy, or Telegram into CPA-specific and DeepSeek-specific implementations.

```text
Character Runtime
      |
      v
LLM Provider Registry
      |
      +-- CPA/Codex
      +-- DeepSeek
      `-- future provider
```

Web Search is a separate capability registry rather than a child of the selected LLM provider.

Canonical transcript, Structured Memory, Character State, Persona Core, and relationship history remain shared across providers. Switching provider must not create a second character identity or continuity stream.

## 3. Implemented Stage 1

### 3.1 LLM provider foundation

Implemented and production-validated:

- CPA/Codex remains default;
- DeepSeek is selectable;
- selection persists per chat;
- `/provider`, `/provider cpa`, `/provider deepseek` use a local deterministic control path;
- exact/high-confidence natural-language controls such as `切到 DeepSeek` / `换回 Codex` use the same control path;
- switching does not depend on the currently selected LLM being healthy;
- switching does not reset Persona, transcript, Structured Memory, or Character State;
- provider/model status is inspectable without exposing secrets;
- task-local selection prevents concurrent chats from leaking provider state into each other.

### 3.2 Capability-aware text and vision routing

Current DeepSeek production profile:

```text
text   -> deepseek-v4-pro
vision -> deepseek-v4-flash-vision-exp
```

A user selecting DeepSeek does not manually switch models before sending an image. The runtime chooses the configured vision-capable model when image input is present.

### 3.3 Independent Web Search provider selection

```text
Character Tool Dispatcher
        |
        v
WebSearchProvider Registry
        |
        +-- CPA hosted web_search
        +-- DeepSeek hosted web_search
        `-- future direct search provider
```

The LLM and Web Search provider can be selected independently.

DeepSeek hosted Web Search was validated against the real Responses API. Its `web_search_call.action.url` output is accepted as source evidence while the runtime continues to reject source-less synthesis as a successful search.

Production regression work also established:

- recent-past phrases such as `昨晚`, `昨天`, `前天` can trigger fresh lookup for external facts;
- questions about Amadeus' own search capability do not auto-search unless explicitly requested;
- hosted search receives configured local date/time so `今天` / `昨天` / `昨晚` are resolved against local runtime time.

## 4. Production baseline

```text
AMADEUS_LLM_PROVIDER=cpa
AMADEUS_WEB_SEARCH_PROVIDER=cpa
```

DeepSeek is configured and selectable, but does not change the default path.

**Automatic failover is OFF.** DeepSeek is an alternate provider, not a silent automatic backup.

Provider credentials remain private runtime material. Development/pre-production capability credentials stay separate from long-lived production configuration until validated and deliberately promoted.

## 5. Validation completed

Stage 1 passed:

1. deterministic unit/regression coverage;
2. Ruff, strict mypy, pytest, package build;
3. Server Dev exact-head validation;
4. real DeepSeek text smoke;
5. real DeepSeek vision smoke;
6. real DeepSeek hosted Web Search smoke with source evidence;
7. non-polling runtime integration for registration, persistence, aliases, chat isolation, independent LLM/Web switching;
8. guarded production deployment preserving one-long-poller invariant;
9. Telegram production smoke for provider switching and live Web Search;
10. production regression for Web trigger/time anchoring.

Persona Core was not modified merely to accommodate DeepSeek.

## 6. Completed operational follow-up: deploy/network robustness

During rollout, the production server's local proxy path twice produced transient GitHub TLS termination (`SSL_ERROR_ZERO_RETURN`) during the initial `git fetch`.

#91 added bounded retry to **only** that initial fetch:

```text
attempt 1 -> wait 2s
attempt 2 -> wait 4s
attempt 3 -> fail normally
```

All later deployment steps remain fail-fast. Existing clean-worktree, fast-forward, exact-main, config, service-state, and one-poller guards remain intact.

This item is **DONE** and should not be conflated with automatic model-provider failover.

## 7. Next provider-related priority: Web Search latency

Production live search is functionally correct but noticeably slower than ordinary chat.

Before changing routing/timeouts/models, measure:

- trigger-to-search-start latency;
- hosted-search upstream latency;
- number of search/open-page actions;
- evidence parsing time;
- Character generation latency after evidence retrieval;
- CPA versus DeepSeek search latency;
- timeout/source-count trade-offs.

Do not remove source validation merely to make search faster.

## 8. Automatic failover remains deferred

Desired eventual shape:

```text
preferred provider request
        |
        +-- success -> normal path
        |
        `-- clearly retryable provider failure
                -> bounded fallback provider
                -> record fallback reason/provider
```

Future automatic failover must distinguish at minimum:

- connection/timeout;
- upstream 5xx/unavailability;
- authentication/configuration;
- rate limits;
- invalid request/schema incompatibility;
- capability mismatch.

Required safeguards:

```text
bounded attempts
cooldown / circuit breaker
no provider ping-pong
fallback telemetry
no blind retry after possible external side effect
```

Do not silently reroute arbitrary schema/content failures.

## 9. Future direct Web Search provider

CPA and DeepSeek provide two hosted search paths. The `WebSearchProvider` boundary should remain open to a future direct provider such as Brave/Tavily only if it materially improves:

- independence from an LLM vendor's hosted-search stack;
- latency;
- provenance quality;
- predictable cost;
- outage isolation.

Any new provider must return the same bounded evidence contract and pass real capability smoke before production use.

## 10. Future role-specific cognitive provider routing

Potential targets:

```text
Conversation Policy
Character Generator
Memory Archivist
Retrospective
Autonomy Planner / Generator
Spontaneity
```

Do **not** implement this simply because whole-runtime provider switching exists. It increases attribution and routing complexity and should require evaluation evidence showing a concrete fidelity/latency/cost/reliability benefit.

## 11. Persona and evaluation policy

CPA/Codex remains the behavioral production baseline.

Provider comparison sequence:

```text
1. preserve functional correctness
2. collect comparable CPA/DeepSeek conversations
3. evaluate character fidelity + failure modes
4. tune provider/model settings only where evidence justifies it
5. change Persona Core only for character reasons, never provider plumbing
```

Provider-generated visible output is never automatically promoted into persistent fact or Canon truth.

## 12. Observability requirements

Routing should remain inspectable without exposing secrets.

Useful fields include:

```text
selected_llm_provider
selected_text_model
selected_vision_model
actual_generator_provider/model
web_search_provider
search_latency
fallback_used             future
fallback_reason_class     future
```

Do not log API keys, bearer headers, full environments, or sensitive request bodies.

## 13. Current definition of done

Stage 1 is complete and remains accepted while these invariants hold:

1. CPA/Codex remains the default and passes validation.
2. DeepSeek can be selected from Telegram without server access.
3. Selection persists per chat and switching does not depend on a healthy LLM.
4. Text turns work through both configured providers.
5. Image turns retain visual understanding through a configured vision model.
6. Web Search selection is independent from Character LLM selection.
7. A non-CPA live-search path returns real source evidence.
8. Routing is visible without leaking credentials.
9. Persona Core / Memory / Character State remain shared and provider-neutral.
10. Automatic failover and per-cognitive-module routing remain deferred unless separately justified.

#89 delivered the foundation, #90 corrected production Web trigger/time behavior, and #91 hardened transient deploy fetch behavior. See `49-multi-provider-production-closeout.md` for exact validation/production evidence.
