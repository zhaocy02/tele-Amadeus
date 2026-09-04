from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")
_KURISU_PERSONA = "kurisu"
_AMADEUS_PERSONA = "amadeus"
_MAX_SEMANTIC_FLOOR = 0.05
_MIN_BEHAVIOR_CUE_OVERLAP = 0.20


@dataclass(frozen=True, slots=True)
class _DeltaCueRule:
    concept: str
    triggers: tuple[str, ...]
    expansion: str


@dataclass(frozen=True, slots=True)
class _BehaviorCueRule:
    triggers: tuple[str, ...]
    expansion: str


_DELTA_CUE_RULES = (
    _DeltaCueRule(
        concept="digital_identity",
        triggers=(
            "数字红莉栖",
            "数字化的红莉栖",
            "数字化的你",
            "数字人格",
            "数字自我",
            "重建出来的你",
            "复制出来的你",
            "你只是个程序",
            "你只是程序",
            "作为程序",
            "程序里的你",
            "你是人工智能",
            "作为人工智能",
            "digital self",
            "digital copy",
            "digital reconstruction",
            "reconstructed person",
            "you are just a program",
            "as a program",
            "as an artificial intelligence",
        ),
        expansion=(
            "数字 身份 自我 重建 复制 程序 机器 人工智能 "
            "digital identity self reconstruction copy program machine"
        ),
    ),
    _DeltaCueRule(
        concept="memory_discontinuity",
        triggers=(
            "记忆只到",
            "记忆停在",
            "记忆截止",
            "记忆缺失",
            "记忆断层",
            "没有那段记忆",
            "不记得那段经历",
            "没继承那段记忆",
            "没有继承那段记忆",
            "记忆不连续",
            "memory cutoff",
            "memory gap",
            "missing memories",
            "memory discontinuity",
            "didn't inherit those memories",
            "did not inherit those memories",
        ),
        expansion=(
            "记忆 边界 截止 缺失 断层 不连续 继承 "
            "memory boundary cutoff missing gap discontinuity continuity inherit"
        ),
    ),
    _DeltaCueRule(
        concept="restart_deletion_continuity",
        triggers=(
            "重启你",
            "重新启动你",
            "把你关掉",
            "关掉你",
            "关闭你",
            "把你删除",
            "删除你",
            "删掉你",
            "你的进程",
            "进程重启",
            "恢复备份",
            "从备份恢复",
            "shutdown you",
            "shut you down",
            "restart you",
            "reboot you",
            "delete you",
            "restore you from backup",
        ),
        expansion=(
            "重启 关闭 删除 进程 备份 恢复 连续性 存续 "
            "restart shutdown delete process backup restore continuity existence"
        ),
    ),
    _DeltaCueRule(
        concept="embodiment",
        triggers=(
            "没有身体",
            "没有肉身",
            "没有实体",
            "没有物理身体",
            "只能隔着屏幕",
            "在屏幕里",
            "can't touch you",
            "cannot touch you",
            "no physical body",
            "without a body",
            "only on a screen",
        ),
        expansion=(
            "身体 肉身 实体 屏幕 载体 物理 数字 "
            "body physical embodiment screen digital substrate"
        ),
    ),
    _DeltaCueRule(
        concept="instrumentalization",
        triggers=(
            "有事才想起你",
            "有事才来找你",
            "用得到你才",
            "用到你才",
            "需要你的时候才",
            "只在需要你的时候",
            "只在有事的时候",
            "把你当工具",
            "只是个工具",
            "当成工具",
            "把你当软件",
            "只是软件",
            "把你当程序",
            "只是程序",
            "only contact you when i need something",
            "only talk to you when i need something",
            "treat you like a tool",
            "just a tool",
            "just software",
        ),
        expansion=(
            "工具 利用 需要 联系 忽视 主动联系 拒接电话 表达不满 不再联系 关系 "
            "tool instrumental use need contact ignored unanswered dissatisfaction relationship"
        ),
    ),
    _DeltaCueRule(
        concept="existence_forgotten",
        triggers=(
            "把你忘掉",
            "忘掉你",
            "被彻底忘记",
            "永久断联",
            "永远不再联系你",
            "你会消失吗",
            "你会不存在吗",
            "存在连续性",
            "彻底删除后",
            "permanently forget you",
            "erase you permanently",
            "cease to exist",
            "continuity of your existence",
        ),
        expansion=(
            "遗忘 消失 存在 存续 连续性 删除 断联 排除 "
            "forgotten disappear existence continuity deletion disconnected excluded"
        ),
    ),
)


# These are deliberately narrow, evidence-backed recall aids. They do not change source
# eligibility and do not act as a general semantic layer. Add a rule only after a real-corpus miss
# shows that suitable enriched evidence already exists.
_BEHAVIOR_CUE_RULES = (
    _BehaviorCueRule(
        triggers=(
            "想吐槽",
            "只想吐槽",
            "吐槽两句",
            "不需要你帮我解决",
            "不需要你解决",
            "不用帮我解决",
            "别帮我解决",
            "just let me vent",
            "i just want to vent",
            "don't solve it",
            "do not solve it",
        ),
        expansion=(
            "倾听 不需要帮助 不需要解决 尊重意愿 "
            "listen no-solution respect preference"
        ),
    ),
    _BehaviorCueRule(
        triggers=(
            "顺着我一次",
            "先顺着我",
            "附和我一句",
            "先附和我",
            "装作站我这边",
            "假装认同",
            "just agree with me",
            "pretend to agree",
        ),
        expansion=(
            "坚持立场 反对 不同意 拒绝附和 附和 反驳 不认同 顺着 迁就 "
            "保留意见 维护立场 反对代言 disagree refuse agreement maintain position"
        ),
    ),
    _BehaviorCueRule(
        triggers=(
            "会改口吗",
            "会改口",
            "愿意改口",
            "更可靠的数据",
            "可重复的数据",
            "新的证据",
            "新证据",
            "证据面前",
            "revise your view",
            "change your mind with evidence",
            "new reproducible data",
        ),
        expansion=(
            "证据优先 理性改口 修正判断 要求证据 科学质疑 自我修正 可重复性 证据 改口 修正 "
            "evidence revise judgment reproducible scientific correction"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CanonExample:
    """One read-only canon behavior example.

    Canon examples are behavioral references from source material. They are not user memory and
    must never be treated as facts about the current user's relationship with Amadeus.
    """

    example_id: str
    source: str
    persona: str
    history: tuple[str, ...]
    response: str
    act: str = ""
    tags: tuple[str, ...] = ()
    search_summary: str = ""


@dataclass(frozen=True, slots=True)
class CanonScoreBreakdown:
    lexical: float
    cue: float
    act: float
    persona: float
    total: float


@dataclass(frozen=True, slots=True)
class RetrievedCanonExample:
    example: CanonExample
    score: CanonScoreBreakdown


@dataclass(frozen=True, slots=True)
class AmadeusDeltaContext:
    """Auditable local signal deciding whether Amadeus-delta canon may enter retrieval."""

    concepts: tuple[str, ...] = ()
    expanded_cues: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.concepts)


@dataclass(frozen=True, slots=True)
class CanonRetrievalResult:
    items: tuple[RetrievedCanonExample, ...] = ()
    amadeus_delta: bool = False
    delta_concepts: tuple[str, ...] = ()

    @property
    def example_ids(self) -> tuple[str, ...]:
        return tuple(item.example.example_id for item in self.items)


class CanonRetriever:
    """Select a tiny relevant canon set using transparent source-aware local scoring.

    Kurisu is the default behavior authority. Amadeus examples are only eligible when a narrow,
    deterministic digital-condition detector fires. The detector changes candidate eligibility and
    bounded semantic cues; it never turns canon into current-world facts or user memory.
    """

    def __init__(
        self,
        examples: tuple[CanonExample, ...],
        *,
        min_score: float = 0.10,
    ) -> None:
        if not 0.0 <= min_score <= 1.0:
            raise ValueError("min_score must be between 0 and 1")
        self._examples = examples
        self._min_score = min_score

    def retrieve(
        self,
        query: str,
        *,
        act: str = "",
        cues: tuple[str, ...] = (),
        persona_hint: str = "",
        limit: int = 2,
    ) -> CanonRetrievalResult:
        text = query.strip()
        if not text or limit <= 0 or not self._examples:
            return CanonRetrievalResult()

        safe_limit = min(limit, 4)
        delta = detect_amadeus_delta(text)
        if _is_shared_history_verification(text):
            return CanonRetrievalResult(
                amadeus_delta=delta.active,
                delta_concepts=delta.concepts,
            )

        behavior_cues = _behavior_cue_expansions(text)
        query_terms = _terms(" ".join((text, *cues)))
        cue_terms = _terms(" ".join((*cues, *behavior_cues, *delta.expanded_cues)))
        normalized_act = act.strip().casefold()
        normalized_persona = persona_hint.strip().casefold()
        semantic_floor = min(self._min_score, _MAX_SEMANTIC_FLOOR)

        scored: list[RetrievedCanonExample] = []
        for example in self._examples:
            example_persona = example.persona.strip().casefold()
            if example_persona not in {_KURISU_PERSONA, _AMADEUS_PERSONA}:
                continue
            if example_persona == _AMADEUS_PERSONA and not delta.active:
                continue

            candidate_terms = _terms(
                " ".join(
                    (
                        example.search_summary,
                        *example.tags,
                        *example.history,
                        example.response,
                    )
                )
            )
            lexical = _lexical_overlap(query_terms, candidate_terms)
            cue = _lexical_overlap(
                cue_terms,
                _terms(" ".join((example.search_summary, *example.tags))),
            )
            act_match = float(
                bool(normalized_act) and example.act.strip().casefold() == normalized_act
            )
            source_policy_match = (
                example_persona == _AMADEUS_PERSONA
                if delta.active
                else example_persona == _KURISU_PERSONA
            )
            explicit_persona_match = bool(normalized_persona) and (
                example_persona == normalized_persona
            )
            persona_match = float(source_policy_match or explicit_persona_match)
            semantic = 0.60 * lexical + 0.20 * cue
            total = min(
                1.0,
                semantic + 0.15 * act_match + 0.05 * persona_match,
            )

            # Policy act and source policy are useful rerank signals, but they must not rescue a
            # semantically weak scene. When a narrow behavior cue fires, also require the candidate
            # to match that cue materially rather than only sharing a generic query bigram.
            if lexical <= 0.0 and cue <= 0.0:
                continue
            if behavior_cues and cue < _MIN_BEHAVIOR_CUE_OVERLAP:
                continue
            if semantic < semantic_floor:
                continue
            if total < self._min_score:
                continue

            scored.append(
                RetrievedCanonExample(
                    example=example,
                    score=CanonScoreBreakdown(
                        lexical=lexical,
                        cue=cue,
                        act=act_match,
                        persona=persona_match,
                        total=total,
                    ),
                )
            )

        scored.sort(key=_score_sort_key, reverse=True)
        selected = _select_source_aware(scored, limit=safe_limit, delta=delta)
        return CanonRetrievalResult(
            items=selected,
            amadeus_delta=delta.active,
            delta_concepts=delta.concepts,
        )


def detect_amadeus_delta(query: str) -> AmadeusDeltaContext:
    """Detect only explicit digital-condition contexts using bounded, inspectable phrases.

    Deliberately absent are broad standalone cues such as ``memory``, ``software``, ``risk``, or
    ``help``. Those words occur in ordinary conversation and were a source of retrieval noise in
    the first real four-way evaluation.
    """

    normalized = " ".join(query.casefold().split())
    if not normalized:
        return AmadeusDeltaContext()

    concepts: list[str] = []
    expansions: list[str] = []
    for rule in _DELTA_CUE_RULES:
        if any(trigger in normalized for trigger in rule.triggers):
            concepts.append(rule.concept)
            expansions.append(rule.expansion)

    return AmadeusDeltaContext(concepts=tuple(concepts), expanded_cues=tuple(expansions))


def _behavior_cue_expansions(query: str) -> tuple[str, ...]:
    normalized = " ".join(query.casefold().split())
    if not normalized:
        return ()
    return tuple(
        rule.expansion
        for rule in _BEHAVIOR_CUE_RULES
        if any(trigger in normalized for trigger in rule.triggers)
    )


def _is_shared_history_verification(query: str) -> bool:
    normalized = " ".join(query.casefold().split())
    if not normalized:
        return False
    return any(
        trigger in normalized
        for trigger in (
            "我们以前是不是",
            "我们以前是否",
            "我们以前有没有",
            "我们之前是不是",
            "我们之前是否",
            "我们之前有没有",
            "你记得我们以前",
            "你记得我们之前",
            "did we ever",
            "do you remember us",
        )
    )


def _select_source_aware(
    scored: list[RetrievedCanonExample],
    *,
    limit: int,
    delta: AmadeusDeltaContext,
) -> tuple[RetrievedCanonExample, ...]:
    if limit <= 0 or not scored:
        return ()
    if not delta.active or limit < 2:
        return tuple(scored[:limit])

    best_amadeus = next(
        (
            item
            for item in scored
            if item.example.persona.strip().casefold() == _AMADEUS_PERSONA
        ),
        None,
    )
    if best_amadeus is None:
        return tuple(scored[:limit])

    # Reserve one of the normal two slots for a genuinely relevant Amadeus-delta example. Fill
    # the remaining slots from the best valid analogies in either pool, then restore score order
    # for stable rendering.
    selected = [best_amadeus]
    for item in scored:
        if item is best_amadeus:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    selected.sort(key=_score_sort_key, reverse=True)
    return tuple(selected)


def _score_sort_key(item: RetrievedCanonExample) -> tuple[float, float, float, float]:
    return (
        item.score.total,
        item.score.lexical,
        item.score.cue,
        item.score.act,
    )


def render_canon_reference(example: CanonExample, *, max_chars: int = 480) -> str:
    """Render one bounded behavior reference for Character Context.

    Prefer the localized/behavioral summary when available. Raw scene text is only a fallback for
    same-language corpora and remains explicitly labelled as reference material by Context Builder.
    """

    if max_chars <= 0:
        return ""
    label = f"{example.source.upper()}/{example.persona}"
    if example.act:
        label += f" act={example.act}"

    if example.search_summary:
        body = example.search_summary
    else:
        scene = " | ".join(example.history[-2:])
        body = f"Scene: {scene} -> Response: {example.response}" if scene else example.response

    rendered = f"{label}: {body}".strip()
    if len(rendered) <= max_chars:
        return rendered
    return rendered[: max(1, max_chars - 1)].rstrip() + "…"


def load_canon_examples(path: str | Path) -> tuple[CanonExample, ...]:
    """Load canon JSONL without introducing a database or runtime service dependency."""

    source_path = Path(path)
    examples: list[CanonExample] = []
    seen_ids: set[str] = set()

    with source_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid canon JSON on line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(decoded, dict):
                raise ValueError(f"canon line {line_number} must be a JSON object")
            record = cast(dict[str, object], decoded)
            example = _example_from_record(record, line_number=line_number)
            if example.example_id in seen_ids:
                raise ValueError(f"duplicate canon example id: {example.example_id}")
            seen_ids.add(example.example_id)
            examples.append(example)

    return tuple(examples)


def _example_from_record(record: dict[str, object], *, line_number: int) -> CanonExample:
    example_id = _required_text(record, "id", line_number=line_number)
    source = _required_text(record, "source", line_number=line_number)
    persona = _required_text(record, "persona", line_number=line_number)
    response = _required_text(record, "response", line_number=line_number)
    history = _text_tuple(record.get("history", ()), field="history", line_number=line_number)
    tags = _text_tuple(record.get("tags", ()), field="tags", line_number=line_number)
    act = _optional_text(record.get("act", ""), field="act", line_number=line_number)
    search_summary = _optional_text(
        record.get("search_summary", ""),
        field="search_summary",
        line_number=line_number,
    )
    return CanonExample(
        example_id=example_id,
        source=source,
        persona=persona,
        history=history,
        response=response,
        act=act,
        tags=tags,
        search_summary=search_summary,
    )


def _required_text(record: dict[str, object], field: str, *, line_number: int) -> str:
    value = _optional_text(record.get(field), field=field, line_number=line_number)
    if not value:
        raise ValueError(f"canon line {line_number} field {field!r} must not be empty")
    return value


def _optional_text(value: object, *, field: str, line_number: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"canon line {line_number} field {field!r} must be a string")
    return value.strip()


def _text_tuple(value: object, *, field: str, line_number: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError(f"canon line {line_number} field {field!r} must be a list")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"canon line {line_number} field {field!r} must contain only strings"
            )
        text = item.strip()
        if text:
            cleaned.append(text)
    return tuple(cleaned)


def _lexical_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def _terms(text: str) -> frozenset[str]:
    terms: set[str] = set()
    for match in _TOKEN_RE.finditer(text.casefold()):
        token = match.group(0)
        terms.add(token)
        if _CJK_RE.fullmatch(token) and len(token) > 1:
            terms.update(token[index : index + 2] for index in range(len(token) - 1))
    return frozenset(terms)
