# 49. Multi-provider production closeout

Status: **implemented, production-validated, and closed as a foundation stage**  
Date: **2026-09-04**

## 1. What this stage delivered

Amadeus now has a provider-neutral runtime boundary instead of treating CPA/Codex as the only possible model/search path.

The production baseline is intentionally conservative:

```text
Character Runtime
      |
      +-- LLM Provider Registry
      |      +-- CPA/Codex      default
      |      `-- DeepSeek       selectable alternate
      |
      +-- Vision capability routing
      |      `-- provider/model selected from the current LLM profile
      |
      `-- WebSearchProvider Registry
             +-- CPA hosted web_search      default
             `-- DeepSeek hosted web_search selectable alternate
```

The LLM and Web Search selections are independent. Selecting DeepSeek for Character generation does not force Web Search onto DeepSeek, and vice versa.

Provider switching does not create a second Persona, memory store, Character State, transcript, or relationship history.

## 2. Production provider matrix

Current production configuration:

| Capability | CPA/Codex | DeepSeek |
| --- | --- | --- |
| Character text | default path | `deepseek-v4-pro` |
| Character image turn | configured CPA/Codex profile | `deepseek-v4-flash-vision-exp` |
| Hosted Web Search | default search provider | selectable search provider |
| Per-chat selection | yes | yes |
| Local deterministic switch | yes | yes |
| Automatic failover | **off** | **off** |

The configured production defaults remain:

```text
AMADEUS_LLM_PROVIDER=cpa
AMADEUS_WEB_SEARCH_PROVIDER=cpa
```

DeepSeek being configured does **not** mean that it is an automatic backup. Current failover is explicit/manual only.

## 3. Runtime control surface

Telegram controls:

```text
/provider
/provider cpa
/provider deepseek
/provider web cpa
/provider web deepseek
```

`/provider` reports the selected LLM provider, text/vision model profile, selected Web Search provider, available providers, and the current automatic-failover state.

A small exact/high-confidence natural-language control surface is also local and deterministic, including forms such as:

```text
切到 DeepSeek
换回 Codex
切回 CPA
```

Longer conversational turns such as `你觉得切到 DeepSeek 怎么样` remain ordinary Character dialogue rather than being silently interpreted as control commands.

Provider-switch commands are control-plane operations. They do not depend on the currently selected LLM being healthy and do not reset continuity state.

## 4. Character-runtime boundaries preserved

The provider work deliberately did not fork the Character Runtime.

The following remain shared and provider-neutral:

- Persona Core;
- canonical transcript;
- Structured Memory and provenance;
- Character State;
- Conversation Policy contract;
- Canon behavior examples;
- Archivist / Retrospective state transitions;
- autonomy and spontaneity semantics.

The provider registry sits below these cognitive/runtime boundaries and above concrete Responses-compatible adapters.

Character-visible model output still cannot automatically become authoritative persistent fact.

## 5. Vision behavior

When the selected provider is DeepSeek:

```text
text-only request
  -> deepseek-v4-pro

request containing an image
  -> deepseek-v4-flash-vision-exp
```

The user does not manually switch models before sending a photo.

Vision bytes/base64 remain ephemeral. The transcript stores only bounded photo metadata/caption information under the existing Vision boundary, and an unconfirmed visual inference is not automatically promoted to long-term user fact.

## 6. Independent Web Search

Web Search has its own provider registry and per-chat preference.

The direct-turn flow is now:

```text
current user message
  -> conservative local trigger
  -> selected WebSearchProvider
  -> hosted web_search
  -> bounded source evidence + optional neutral synthesis
  -> EXTERNAL WEB EVIDENCE context
  -> Character Generator through selected LLM provider
```

A successful search still requires source provenance. Plain model text without recoverable source evidence is not accepted as a successful Web Search result.

For DeepSeek Responses, real server probing showed source evidence in `web_search_call.action.url` for page-open actions. The parser accepts that source shape while retaining the same source-evidence requirement used by the CPA path.

### Relative-date anchoring

Production smoke exposed an upstream date ambiguity around `今天` after local midnight. Hosted search now receives the configured local runtime date/time and interprets relative words such as:

```text
今天
昨天
昨晚
```

against that configured local clock.

The current single-user runtime reuses the existing validated IANA timezone setting (`AMADEUS_AUTONOMY_TIMEZONE`) as the shared local-time basis.

### Trigger regression fixed in production

The production regression that led to the Web Search hotfix is preserved conceptually:

```text
OpenAI昨晚是不是服务器发生了故障？有相关新闻吗
  -> fresh external fact -> search

我应该给你更新了搜索网页的功能，现在没法正常使用吗？
  -> question about Amadeus' own capability -> do not auto-search

你能联网查一下 OpenAI 昨晚有没有故障吗？
  -> explicit request -> search
```

## 7. Validation evidence

### #89 — multi-provider foundation

Merged main commit:

```text
3b79be37948485654765a291be699d0c36bb1427
```

Server Dev and CI validated the provider registry, capability routing, per-chat persistence, independent LLM/Web selection, aliases, context isolation, and Telegram control path.

Real DeepSeek capability smokes passed on the exact feature head before merge:

```text
TEXT_CAPABILITY=PASS
VISION_CAPABILITY=PASS
WEB_SEARCH_CAPABILITY=PASS
PROVIDER_CAPABILITY=PASS
```

Observed real results included:

```text
text_response=AMADEUS_PROVIDER_OK
vision_response=RED
web_sources>=2
```

A separate non-polling runtime integration probe passed:

```text
REGISTRATION=PASS
DEFAULT_SELECTION=PASS
INDEPENDENT_LLM_SWITCH=PASS
INDEPENDENT_WEB_SWITCH=PASS
CHAT_ISOLATION=PASS
PERSISTENCE=PASS
ALIASES=PASS
RUNTIME_PROVIDER_INTEGRATION=PASS
```

### #90 — production Web Search trigger/time-anchor hotfix

Merged main commit:

```text
71a71b6e8a3a98e72ae44435087707860ed9d216
```

Validated:

- recent-past trigger cues such as `昨晚` / `昨天` / `前天`;
- suppression of non-explicit capability-meta false positives;
- explicit search remains highest-priority trigger;
- local-time anchoring for hosted search;
- real DeepSeek Web Search still returns source evidence.

Telegram production regression then confirmed automatic recent-news search and correct local-date interpretation.

### #91 — production deploy fetch retry

Merged main commit:

```text
b679e9afeab61737336a7bd68e07d8bc34c4fd28
```

The guarded production helper now retries **only** its initial `git fetch origin` up to three attempts, with bounded 2-second / 4-second backoff. All later deployment steps remain fail-fast.

This addresses observed transient `SSL_ERROR_ZERO_RETURN` failures on the server's local proxy path without hiding persistent authentication/network faults.

## 8. Production activation and safety

Production checkout:

```text
/opt/amadeus-bot
```

Expected service state:

```text
telegram-codex-cpa-bot.service  inactive
amadeus-telegram-bot.service    active
cli-proxy-api.service           active
```

The one-long-poller invariant remains absolute: do not start a second process using the production Telegram Bot token.

Routine deployment remains:

```bash
cd /opt/amadeus-bot
bash scripts/deploy-prod.sh
```

For code/docs synchronization that does not require reloading runtime behavior:

```bash
bash scripts/deploy-prod.sh --skip-restart
```

Production deploy accepts merged `main` only, requires a clean production working tree, fast-forwards only, validates exact `origin/main`, installs/checks config, and checks service state.

## 9. Secret lifecycle

Secrets are organized by lifecycle rather than by creating one file for every provider.

Long-lived production configuration:

```text
/opt/amadeus-bot/.env
```

Development/pre-production capability credentials:

```text
$HOME/.config/amadeus/provider-smoke.env
```

The private smoke file is not a production config source of truth. A provider credential is promoted into production `.env` only after real capability validation and an explicit activation decision.

Never print, commit, paste into PRs, or include in diagnostics:

- Telegram tokens;
- CPA/API bearer keys;
- DeepSeek API keys;
- complete `.env` content;
- complete process environments;
- real memory/runtime databases.

## 10. Provider capability smoke

Current generic helper:

```bash
bash scripts/provider-capability-smoke.sh \
  --provider deepseek \
  --capability text|vision|web|all \
  --credentials-env "$HOME/.config/amadeus/provider-smoke.env"
```

The helper is designed to avoid Telegram authority and runtime-state side effects. It must not initialize a second production poller.

Historical CPA-specific smoke helpers remain useful for their original capability records, but new multi-provider validation should prefer the generic provider capability helper where applicable.

## 11. What remains intentionally deferred

### Automatic provider failover

Still **off** and low priority.

Any future implementation must distinguish connection/timeout, upstream 5xx, auth/config, rate limits, invalid request/schema mismatch, and capability mismatch. It also needs bounded attempts, cooldown/circuit-breaker behavior, no provider ping-pong, and explicit telemetry.

### Per-cognitive-module provider routing

Still a future experiment only. Policy, Generator, Archivist, Retrospective, autonomy, and spontaneity currently share the selected Character LLM provider for a chat turn/runtime context.

Introduce finer routing only if evaluation data shows a concrete benefit in character fidelity, latency, cost, or failure isolation.

### Direct third-party search provider

The `WebSearchProvider` boundary can later admit Brave, Tavily, or another direct provider. There is no requirement to add one merely for provider count; it should solve a demonstrated latency, independence, provenance, cost, or outage-isolation problem.

## 12. Known follow-up: Web Search latency

Production Web Search is functionally correct but noticeably slower than ordinary conversation.

Do not optimize it by weakening source validation.

The next performance investigation should separately measure:

```text
trigger -> search start
hosted-search upstream latency
number of search/open-page actions
evidence parsing
Character generation after evidence
Telegram delivery
```

Compare CPA and DeepSeek search paths before changing timeouts, source counts, model effort, or routing behavior.

## 13. Documentation lineage

These documents describe the earlier CPA-only Web Search stages and should be read as historical implementation records:

- `41-infra-cpa-native-web-search-capability.md`;
- `42-int-web-search-runtime-v1.md`;
- `43-web-search-v1-production-verification.md`.

The current provider-resilience architecture and roadmap are defined by:

- `03-target-architecture.md`;
- `48-provider-resilience-plan.md`;
- this closeout document.

## 14. Closeout decision

The multi-provider foundation is complete.

The accepted baseline is:

```text
CPA/Codex remains default
+ DeepSeek text/vision is production-selectable
+ Web Search provider is independently selectable
+ provider selection persists per chat
+ local switch controls work without LLM availability
+ source provenance remains mandatory for successful Web Search
+ Persona/Memory/State continuity remains shared
+ automatic failover remains off
```

Future provider work should extend this baseline incrementally rather than turning it into a second Character Runtime or a complex orchestration framework.