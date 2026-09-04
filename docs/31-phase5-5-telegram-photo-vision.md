# Phase 5.5 — Telegram Photo / Vision

## Status

Phase 5.5 is **complete and production-verified**.

Merged implementation commit:

```text
1866c130a380b822338288e10c9d91c905cbf4c7
```

Final gate status:

```text
Gate A  CPA / provider input_image capability smoke     PASS
Gate B  transport-neutral multimodal request contract  PASS
Gate C  Telegram photo parse/getFile/bounded download  PASS
Gate D  Character Runtime multimodal integration       PASS
Gate E  production deploy + real Telegram photo smoke  PASS
```

Gate A evidence from Server Dev:

```text
model=gpt-5.6-sol
response=RED
VISION_CAPABILITY=PASS
```

Gate E then proved the complete real Telegram path using both a no-caption photo and a captioned photo. Amadeus correctly identified concrete visual details that were not present in the caption, so the production path is verified end to end rather than merely accepting photo-shaped updates.

Detailed production evidence is recorded in [`32-phase5-5-production-verification.md`](32-phase5-5-production-verification.md).

## 1. First-version scope

Phase 5.5 v1 supports:

```text
Telegram private photo
+ optional caption
-> authorization
-> durable staging of opaque photo metadata
-> select one Telegram photo size
-> getFile
-> bounded binary download
-> ephemeral multimodal Character input
-> normal Character text reply
```

Explicitly out of scope for this slice:

```text
video
video note
animation / GIF
document images
stickers
media-group / album orchestration
outbound image generation
image editing
persistent image library
OCR-specific pipeline
```

These remain later, evidence-driven extensions rather than part of the first multimodal rollout.

## 2. Provider capability gate

The repeatable capability command remains available from Server Dev:

```bash
cd /opt/amadeus-bot-dev
bash scripts/vision-provider-smoke.sh
```

The helper:

- refuses the production checkout;
- uses the Server Dev checkout's own `.venv`;
- reads existing provider settings without printing them;
- removes Telegram credentials and forces long polling off;
- does not initialize Telegram or touch runtime SQLite.

The probe sends an embedded red PNG as Responses `input_image`. The current CPA + `gpt-5.6-sol` returned `RED`, proving that the configured compatibility path and model accept and interpret image input.

## 3. Transport-neutral multimodal boundary

Telegram-specific photo objects do not enter Character Runtime.

The implementation uses explicit boundaries:

```text
IncomingImageAttachment
  source_id
  width / height
  byte_size_hint
  media_type

DownloadedImage
  data
  media_type

LLMImage
  data
  media_type
```

`IncomingMessage` can carry image attachment metadata while remaining compatible with text-only turns. Character/LLM code does not depend on Telegram `photo[]`, `file_id`, Bot API URLs, or `getFile` wire shapes.

## 4. Authorization, durable inbox, and download safety

Authorization happens before media resolution/download. Non-private and unallowlisted updates never enter the image download path.

For an authorized photo:

```text
parse update
-> choose largest valid PhotoSize by pixel area
-> persist only opaque attachment metadata
-> normal FIFO router/delivery
-> getFile(file_id)
-> bounded streamed download
```

The durable inbox adds:

```text
attachments_json TEXT NOT NULL DEFAULT '[]'
```

This is a backward-compatible, metadata-only extension. It does not store image bytes or base64.

The first-version hard image limit is:

```text
8 MiB per staged photo
```

The gateway checks size hints, `getFile` size, HTTP `Content-Length` when supplied, and actual streamed bytes. Oversized, empty, malformed, or failed downloads abort with sanitized errors; tokenized Telegram file URLs are not surfaced in errors/logs.

## 5. Provider / Character behavior

`LLMMessage` remains text-compatible and can additionally carry ephemeral `LLMImage` values.

Text-only messages retain the old wire representation. A current image turn is serialized only at provider-request time as:

```text
current user input_text
+ input_image data URL
```

The data URL exists only in memory for that outbound provider request.

Only the **current** user message may carry image bytes. Historical recent-conversation messages reject retained image payloads, preventing binary image data from becoming long-lived Character context.

Generation guidance explicitly treats visual inference as fallible and explicit caption/user confirmation as stronger evidence than an uncertain visual guess.

## 6. Conversation Policy behavior

Phase 5.5 v1 deliberately does **not** add a second vision call to Conversation Policy.

Policy sees the canonical photo marker plus caption; Character Generation sees the actual image. This avoids automatically doubling multimodal calls and latency.

The first production examples routed as:

```text
photo without caption
  fast_neutral_chat

photo + "你看到了什么？"
  fast_explicit_task
```

Phase 5.4 routing telemetry should determine whether later modality-specific deliberate routing is actually justified.

## 7. Transcript, telemetry, and memory semantics

### Transcript

Raw image bytes are ephemeral. Persisted user transcript is bounded text:

```text
[User sent a photo]
```

or:

```text
[User sent a photo]
Caption: <original caption>
```

### Phase 5.4 telemetry

Telemetry remains content-light and joins back to the canonical transcript when an operator explicitly asks for routing/slow-turn previews.

Production verification showed only the bounded photo markers/caption. No image bytes, base64, Telegram file ID, or tokenized file URL appeared in the observation report.

### Structured Memory

Raw images do not become long-term memory by default.

For Phase 5.5 v1, automatic Archivist extraction is skipped on image turns. This prevents an unconfirmed visual guess from silently becoming an authoritative durable user fact.

A future image-memory feature must introduce explicit image-derived provenance/confidence and user-confirmation semantics before relaxing this rule.

## 8. FIFO and cancellation semantics

Photo turns use the same router/per-chat lock as ordinary text turns:

```text
same-chat ordinary text/photo turns -> strict FIFO
different chats                     -> may run concurrently
/cancel                              -> bypasses ordinary-turn lock
```

The production photo smoke also showed elevated `queue_wait_ms` for rapidly sent consecutive turns while all turns still completed in order, providing additional evidence that FIFO staging is functioning rather than dropping later messages.

## 9. Validation coverage

Automated coverage includes:

```text
provider input_image serialization
real provider capability probe contract
photo update authorization
largest PhotoSize selection
caption parsing
unauthorized photo rejection before download
metadata-only durable inbox round-trip
getFile + bounded streaming download
max-byte enforcement and token-safe failures
current-image-only Character context
photo + caption canonical transcript marker
photo without caption
text-only regressions
same-chat FIFO and /cancel regressions
```

The final exact Phase 5.5 branch head passed Server Dev install / shell helpers / operator CLIs / Ruff / strict mypy / pytest / build before squash merge.

## 10. Production acceptance — completed

The guarded deploy completed with:

```text
head_after=1866c130a380b822338288e10c9d91c905cbf4c7
v1=inactive
v2=active
cpa=active
install=PASS
config=PASS
restart=PASS
```

Production acceptance then passed:

```text
ordinary text chat works
real photo without caption receives image-grounded Character reply
real photo with caption receives image+caption-grounded Character reply
Phase 5.4 report shows only photo marker/caption
warning_turns=0
no raw image/base64 in transcript or telemetry output
```

## 11. Deployment CA incident

The first deploy attempt stopped safely at `pip install -e .` before config/restart because pip's default Python trust chain could not validate PyPI while PEP 517 attempted to fetch `setuptools>=75`/`wheel`.

The services stayed on the previously running runtime during the failed attempt. A system-CA smoke succeeded with:

```text
PIP_CERT=/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
```

and the guarded deploy then completed normally.

This is a deployment-environment issue, not a Photo/Vision runtime failure. It should be addressed by a separate deployment-hardening change rather than weakening TLS verification.

## 12. Roadmap transition

Phase 5.4 observability continues collecting production evidence in the background.

The active roadmap is now:

```text
DONE  Phase 5.5  Telegram Photo / Vision
NOW   Phase 5.6  conservative Autonomy production pilot
NEXT  Phase 5.7  evidence-driven Character / latency / memory tuning
LATER Phase 6    legacy v1 removal after rollback window is explicitly closed
```

Phase 5.5 did not activate autonomy, remove legacy v1, weaken the one-long-poller invariant, or change production service ownership.
