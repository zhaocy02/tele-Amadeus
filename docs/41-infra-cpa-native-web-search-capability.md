# 41. CPA-native web search capability foundation

## Status

Track: **INFRA / INT foundation**

**Complete and production-proven as the provider foundation for Web Search v1.**

The foundation itself only encapsulates and proves the provider capability. Ordinary direct-turn wiring is implemented separately in `docs/42-int-web-search-runtime-v1.md` and is now also production-verified.

Foundation merge commit:

```text
d5c84c305fb30b9676137bfc7300b13731cebe89
```

Real capability smoke was executed from an isolated detached exact-SHA worktree against the existing CPA provider path and returned:

```text
provider=cpa-native-web-search
sources=3
source_1_domain=developers.openai.com
source_2_domain=openai.com
source_3_domain=platform.openai.com
WEB_SEARCH_CAPABILITY=PASS
SMOKE_RC=0
```

No Brave/Tavily key and no reverse proxy were used.

## Why this replaced the Brave-primary experiment

The first Web Search v1 experiment used a direct Brave Search API adapter. Unit/CI validation passed, but the real Server Dev provider smoke timed out before receiving an HTTP response. Making a reverse proxy the normal production path would add an operational dependency the project does not want.

The existing Amadeus runtime already reaches CLIProxyAPI (CPA) successfully for Responses requests. CPA exposes hosted server-side search through the Responses `web_search` tool and returns `web_search_call` output with source provenance.

The production capability boundary is therefore:

```text
Amadeus server
    -> existing loopback CPA /responses
        -> hosted web_search tool
        -> upstream search execution
    <- web_search_call + source evidence
```

The Amadeus host does not need to reach Brave, Tavily, Google, or Bing directly.

## Provider contract

`CPAWebSearchProvider` implements the small `WebSearchProvider` protocol.

Input:

- one explicit query string only;
- maximum 400 characters / 50 whitespace-delimited words;
- no Character State;
- no Structured Memory;
- no recent transcript;
- no Telegram IDs;
- no hidden policy context.

Output:

- normalized query;
- retrieval timestamp;
- bounded source list (`title`, `url`, optional snippet);
- optional neutral synthesis returned by the hosted-search model;
- provider identity.

A response counts as a successful search only when:

1. CPA returns a real `web_search_call`; and
2. at least one source URL can be recovered from the search-call results/sources or output-text URL citations.

Plain model text without a `web_search_call` is explicitly rejected as non-search output. This prevents the runtime from confusing model prior knowledge with verified external retrieval.

## Authentication and cost boundary

This capability reuses the existing provider settings:

```text
AMADEUS_PROVIDER_BASE_URL
AMADEUS_PROVIDER_API_KEY
AMADEUS_PROVIDER_MODEL
AMADEUS_REQUEST_TIMEOUT_SECONDS
```

It introduces no Brave/Tavily key and no new Amadeus-side search subscription. Search turns may still consume whatever model/tool quota applies to the existing CPA/upstream account; that remains part of the existing provider usage boundary rather than a second search-API billing account.

## Capability smoke

The smoke remains available for future provider/model compatibility checks:

```bash
bash scripts/web-search-provider-smoke.sh
```

For evidence-producing server validation, follow `WORKTREE_WORKFLOW.md`: use a detached exact-SHA task worktree. If task-local dependency installation is blocked by the host environment, the helper supports explicit read-only reuse of a compatible existing interpreter while verifying that `amadeus_bot` imports from the exact-SHA worktree.

The helper:

- refuses the production checkout;
- imports code from the checkout/worktree being tested;
- reads existing provider credentials from the production `.env` only inside a subshell;
- removes Telegram credentials and proactive-process gates before execution;
- never starts polling;
- never opens Amadeus runtime databases;
- prints only provider/source-count/domain information, never credentials or raw response bodies.

A PASS proves the currently configured CPA/model executes hosted web search and returns source provenance. A FAIL should leave the feature disabled; do not silently make a reverse proxy or a second search API the production fallback.

## Foundation non-goals

The foundation deliberately does not own:

- direct-turn search-trigger policy;
- Character Context injection;
- Telegram behavior;
- web search from spontaneity or long-horizon autonomy;
- persistence/schema changes;
- arbitrary URL fetching/browser automation;
- direct conversion of web evidence into long-term memory.

Direct-turn trigger/context wiring is documented in `42-int-web-search-runtime-v1.md`. The remaining non-goals continue to hold after v1 production closeout.
