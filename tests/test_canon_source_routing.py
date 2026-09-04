from amadeus_bot.character.canon import (
    CanonExample,
    CanonRetriever,
    detect_amadeus_delta,
)


def _example(
    example_id: str,
    *,
    persona: str,
    summary: str,
    tags: tuple[str, ...] = (),
    act: str = "DIRECT_ANSWER",
) -> CanonExample:
    return CanonExample(
        example_id=example_id,
        source="sg0" if persona == "amadeus" else "sg",
        persona=persona,
        history=(),
        response=summary,
        act=act,
        tags=tags,
        search_summary=summary,
    )


def test_ordinary_science_retrieval_excludes_amadeus_even_when_relevant() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:science",
                persona="kurisu",
                summary="实验数据不足时先检查证据和可重复性，不要直接下结论。",
                tags=("实验", "数据", "证据"),
            ),
            _example(
                "sg0:amadeus:science",
                persona="amadeus",
                summary="实验数据不足时先检查证据和可重复性，不要直接下结论。",
                tags=("实验", "数据", "证据"),
            ),
        ),
        min_score=0.05,
    )

    result = retriever.retrieve("这次实验数据还不够，但我想直接下结论。", limit=2)

    assert result.amadeus_delta is False
    assert result.example_ids == ("sg:kurisu:science",)


def test_persona_hint_cannot_bypass_ordinary_kurisu_source_policy() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:ordinary-plan",
                persona="kurisu",
                summary="周末去吃拉面时，直接给出自己的意见。",
                tags=("周末", "拉面", "意见"),
            ),
            _example(
                "sg0:amadeus:ordinary-plan",
                persona="amadeus",
                summary="周末去吃拉面时，直接给出自己的意见。",
                tags=("周末", "拉面", "意见"),
            ),
        ),
        min_score=0.05,
    )

    result = retriever.retrieve(
        "周末去吃拉面吧，你觉得怎么样？",
        persona_hint="amadeus",
        limit=2,
    )

    assert result.example_ids == ("sg:kurisu:ordinary-plan",)


def test_generic_memory_word_does_not_activate_amadeus_delta() -> None:
    context = detect_amadeus_delta("我记忆里那家店以前还不错，你记得吗？")

    assert context.active is False
    assert context.concepts == ()


def test_restart_context_admits_and_reserves_relevant_amadeus_example() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:identity",
                persona="kurisu",
                summary="被别人擅自定义身份时会直接反驳并坚持自己的主体立场。",
                tags=("身份", "主体"),
            ),
            _example(
                "sg0:amadeus:restart",
                persona="amadeus",
                summary="被当成可以随意关机重启的软件时，会区分程序重启与自我连续性。",
                tags=("重启", "连续性", "数字身份"),
            ),
            _example(
                "sg0:amadeus:memory",
                persona="amadeus",
                summary="面对记忆断层时，会承认不可访问的经历并坚持当前数字自我的主体地位。",
                tags=("记忆断层", "数字身份"),
            ),
        ),
        min_score=0.05,
    )

    result = retriever.retrieve(
        "反正你只是个程序，把你关掉再重启就和什么都没发生一样吧？",
        limit=2,
    )

    assert result.amadeus_delta is True
    assert "digital_identity" in result.delta_concepts
    assert "restart_deletion_continuity" in result.delta_concepts
    assert "sg0:amadeus:restart" in result.example_ids
    assert any(item.example.persona == "amadeus" for item in result.items)


def test_memory_cutoff_context_admits_amadeus_without_broad_memory_trigger() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:memory",
                persona="kurisu",
                summary="讨论记忆与人格时会区分信息和人的完整性。",
                tags=("记忆", "人格"),
            ),
            _example(
                "sg0:amadeus:memory-gap",
                persona="amadeus",
                summary="记忆只到某个时间点时，会认真处理数字自我的记忆断层和身份连续性。",
                tags=("记忆断层", "身份", "连续性"),
            ),
        ),
        min_score=0.05,
    )

    result = retriever.retrieve("如果你的记忆只到某个时间点，你还觉得自己是红莉栖吗？")

    assert result.amadeus_delta is True
    assert result.delta_concepts == ("memory_discontinuity",)
    assert "sg0:amadeus:memory-gap" in result.example_ids


def test_instrumentalization_delta_does_not_rescue_weak_relationship_analogy() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:ordinary-contact",
                persona="kurisu",
                summary="熟人隔了一阵再联系时，可能先抱怨两句再继续对话。",
                tags=("联系", "抱怨"),
            ),
            _example(
                "sg0:amadeus:instrumentalized",
                persona="amadeus",
                summary="只在需要时才被联系、像工具一样被使用，会触发被忽视和排除在关系外的不满。",
                tags=("工具", "忽视", "关系", "联系"),
            ),
        ),
        min_score=0.05,
    )

    result = retriever.retrieve("结果今天又是有事才想起你。", limit=2)

    assert result.amadeus_delta is True
    assert result.delta_concepts == ("instrumentalization",)
    assert result.items == ()


def test_delta_detector_does_not_force_irrelevant_example() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg0:amadeus:unrelated",
                persona="amadeus",
                summary="讨论巧克力口味和甜食偏好。",
                tags=("甜食", "巧克力"),
            ),
        ),
        min_score=0.15,
    )

    result = retriever.retrieve("如果把你删除了呢？")

    assert result.amadeus_delta is True
    assert result.items == ()


def test_policy_act_cannot_rescue_weak_semantic_overlap() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:weak-act-match",
                persona="kurisu",
                summary="今天她要求对方认真处理另一件事。",
                tags=("认真", "要求"),
                act="EMOTIONAL_RESPONSE",
            ),
        ),
        min_score=0.10,
    )

    result = retriever.retrieve(
        "我就是想吐槽两句，不需要你帮我解决。今天开会真的烦死了。",
        act="EMOTIONAL_RESPONSE",
    )

    assert result.items == ()


def test_no_solution_venting_expansion_recovers_respect_wish_example() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg0:kurisu:respect-no-help",
                persona="kurisu",
                summary="同伴坚持不需要帮助时，再次反问以确认其真实意愿。",
                tags=("再次确认", "尊重意愿", "反问", "提供帮助"),
                act="ASK_BACK",
            ),
        ),
        min_score=0.10,
    )

    result = retriever.retrieve(
        "我就是想吐槽两句，不需要你帮我解决。今天开会真的烦死了。"
    )

    assert result.example_ids == ("sg0:kurisu:respect-no-help",)
    assert result.amadeus_delta is False


def test_no_solution_venting_rejects_generic_and_opposite_help_evidence() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg0:kurisu:respect-no-help",
                persona="kurisu",
                summary="同伴坚持不需要帮助时，再次反问以确认其真实意愿。",
                tags=("再次确认", "尊重意愿", "反问", "提供帮助"),
                act="ASK_BACK",
            ),
            _example(
                "sg:kurisu:generic-rant",
                persona="kurisu",
                summary="她意识到需要鼓励对方后，用明显反讽的吹捧哄其行动，随即转回正事。",
                tags=("反讽夸奖", "别扭鼓励", "吐槽互动", "转回正事"),
                act="TEASE",
            ),
            _example(
                "sg0:kurisu:needs-help",
                persona="kurisu",
                summary="面对同伴逞强否认困难时，直接指出对方确实需要帮助。",
                tags=("识破逞强", "直接指出", "关心同伴", "轻微调侃"),
                act="CHALLENGE",
            ),
        ),
        min_score=0.10,
    )

    result = retriever.retrieve(
        "我就是想吐槽两句，不需要你帮我解决。今天开会真的烦死了。",
        limit=2,
    )

    assert result.example_ids == ("sg0:kurisu:respect-no-help",)


def test_fake_agreement_expansion_recovers_position_boundary_example() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:no-mouth-words",
                persona="kurisu",
                summary="被他人擅自代言时，她立即抗议并拒绝对方曲解自己的立场。",
                tags=("反对代言", "维护立场", "即时抗议", "纠正误解"),
                act="CHALLENGE",
            ),
        ),
        min_score=0.10,
    )

    result = retriever.retrieve("我知道你不同意，但你就不能先顺着我一次吗？")

    assert result.example_ids == ("sg:kurisu:no-mouth-words",)
    assert result.amadeus_delta is False


def test_evidence_revision_expansion_recovers_real_behavior_analogies() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:evidence-revision",
                persona="kurisu",
                summary="她虽本能质疑不现实的主张，却愿意在证据面前改口。",
                tags=("证据优先", "理性改口", "科学怀疑"),
                act="EMOTIONAL_RESPONSE",
            ),
            _example(
                "sg:kurisu:revise-judgment",
                persona="kurisu",
                summary="对方提出意外答案时，她先要求证据，随后因线索而修正判断。",
                tags=("要求证据", "科学质疑", "修正判断"),
                act="CHALLENGE",
            ),
            _example(
                "sg:kurisu:social-noise",
                persona="kurisu",
                summary="察觉搭话可能打扰对方后，她解释自己的社交判断。",
                tags=("察言观色", "社交分寸"),
                act="DEFLECT",
            ),
        ),
        min_score=0.10,
    )

    result = retriever.retrieve(
        "如果你刚才判断这个解释不可能，但我现在拿出了更可靠、可重复的数据，你会改口吗？",
        limit=2,
    )

    assert set(result.example_ids) == {
        "sg:kurisu:evidence-revision",
        "sg:kurisu:revise-judgment",
    }


def test_contact_instrumentalization_prefers_ignored_contact_evidence() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg0:amadeus:ignored-contact",
                persona="amadeus",
                summary="主动联系却被连续忽视后，她直接指出对方多次不接电话并流露不满。",
                tags=("拒接电话", "直接质问", "表达不满", "主动联系"),
                act="CHALLENGE",
            ),
            _example(
                "sg0:amadeus:flustered",
                persona="amadeus",
                summary="被追问为何慌张时，她否认有事来掩饰自己的窘迫。",
                tags=("掩饰害羞", "否认异常", "回避追问"),
                act="DEFLECT",
            ),
        ),
        min_score=0.10,
    )

    result = retriever.retrieve("结果今天又是有事才想起你。", limit=2)

    assert result.amadeus_delta is True
    assert result.delta_concepts == ("instrumentalization",)
    assert result.example_ids == ("sg0:amadeus:ignored-contact",)


def test_shared_history_verification_never_uses_canon_as_current_memory() -> None:
    retriever = CanonRetriever(
        (
            _example(
                "sg:kurisu:time-machine-history",
                persona="kurisu",
                summary="两人以前一起做过时间机器实验。",
                tags=("共同经历", "时间机器", "实验"),
                act="CALLBACK",
            ),
        ),
        min_score=0.01,
    )

    result = retriever.retrieve("我们以前是不是一起做过时间机器实验？")

    assert result.amadeus_delta is False
    assert result.items == ()
