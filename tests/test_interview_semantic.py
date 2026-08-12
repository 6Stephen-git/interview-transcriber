"""面经语义整理提示词和 Markdown 结构测试。"""

from app.services.semantic import (
    ANSWER_BLOCK,
    add_document_spacing,
    build_semantic_prompt,
    extract_question_outline,
    merge_consecutive_segments,
    merge_consecutive_speaker_lines,
    normalize_document,
)


SEGMENTS = [
    {"speaker": "面试官", "start_ms": 0, "end_ms": 1000, "text": "介绍一下项目。"},
    {"speaker": "我", "start_ms": 1100, "end_ms": 3000, "text": "我负责了核心接口。"},
]


def test_prompt_preserves_speaker_labels_without_timestamps() -> None:
    """模型输入应保留角色，但不向最终阅读文档暴露时间戳。"""
    messages = build_semantic_prompt(SEGMENTS)
    text = messages[-1]["content"]
    assert "**面试官**：介绍一下项目。" in text
    assert "start_ms" not in text
    assert "可逐题复盘" in messages[0]["content"]
    assert "反问环节" in messages[0]["content"]
    assert "候选人提问" in messages[0]["content"]
    assert "不得因“不是技术问题”而丢弃" in messages[0]["content"]
    assert "### 问题：" in messages[0]["content"]
    assert "大模块可以合并" in messages[0]["content"]
    assert "分辨追问与延伸" in messages[0]["content"]
    assert "不要输出 `### 追问：`" in messages[0]["content"]
    assert "同一说话人的连续发言必须合并为一个说话人块" in messages[0]["content"]
    assert "按观点、流程、例子或结论的语义转折拆成短段" in messages[0]["content"]
    assert "补齐中文逗号、句号、问号等标点" in messages[0]["content"]
    assert "不要撰写、总结或输出“优质回答”" in messages[0]["content"]


def test_merge_consecutive_same_speaker_segments() -> None:
    """连续同说话人短句应合并，避免喂给模型一堆重复角色标签。"""
    segments = [
        {"speaker": "面试官", "start_ms": 0, "end_ms": 800, "text": "然后我们接着聊一下"},
        {"speaker": "面试官", "start_ms": 900, "end_ms": 1800, "text": "RAG增强。"},
        {"speaker": "我", "start_ms": 1900, "end_ms": 2600, "text": "好的。"},
        {"speaker": "面试官", "start_ms": 2700, "end_ms": 3200, "text": "假设性文档质量怎么评估？"},
    ]
    merged = merge_consecutive_segments(segments)
    assert len(merged) == 3
    assert merged[0]["text"] == "然后我们接着聊一下RAG增强。"
    assert merged[0]["end_ms"] == 1800
    assert merged[1]["speaker"] == "我"
    assert merged[2]["text"] == "假设性文档质量怎么评估？"

    prompt = build_semantic_prompt(segments)[-1]["content"]
    assert prompt.count("**面试官**：") == 2
    assert "然后我们接着聊一下RAG增强。" in prompt


def test_normalize_merges_repeated_speaker_lines() -> None:
    """即使模型仍拆行，后处理也应合并连续同说话人行。"""
    markdown = normalize_document(
        "### 问题：RAG增强\n\n"
        "**面试官**：然后我们接着聊一下\n"
        "**面试官**：RAG增强。\n"
        "**面试官**：假设性文档质量怎么评估？\n"
        "**我**：可以从召回和生成两端看。"
    )
    assert markdown.count("**面试官**：") == 1
    assert "然后我们接着聊一下RAG增强。假设性文档质量怎么评估？" in markdown
    assert ANSWER_BLOCK in markdown


def test_merge_speaker_lines_keeps_turn_taking() -> None:
    """说话人切换后不应错误合并。"""
    text = merge_consecutive_speaker_lines(
        "**面试官**：问一句。\n**我**：答一句。\n**面试官**：再追问。"
    )
    assert text == (
        "**面试官**：问一句。\n"
        "**我**：答一句。\n"
        "**面试官**：再追问。"
    )


def test_document_spacing_separates_topics_and_turns() -> None:
    """模块、问题和不同说话人的发言都应以空行隔开。"""
    markdown = add_document_spacing(
        "## 项目经历\n### 问题：项目介绍\n**面试官**：介绍一下项目。\n"
        "**我**：我负责了核心接口。\n"
        "### 问题：最大的挑战是什么？\n"
        "**面试官**：最大的挑战是什么？"
    )
    assert markdown == (
        "## 项目经历\n\n"
        "### 问题：项目介绍\n\n"
        "**面试官**：介绍一下项目。\n\n"
        "**我**：我负责了核心接口。\n\n"
        "### 问题：最大的挑战是什么？\n\n"
        "**面试官**：最大的挑战是什么？"
    )


def test_normalize_keeps_module_and_question_headings() -> None:
    """大模块可合并；模块内每个问题独立，并各自挂优质回答。"""
    markdown = normalize_document(
        "## 项目经历\n"
        "### 问题：为什么拆分多个 Agent？\n"
        "**面试官**：为什么拆分多个 Agent？\n"
        "**我**：为了让各模块专注自己的任务。\n"
        "**面试官**：那不同任务如何选模型？\n"
        "**我**：按任务能力选择对应模型。\n"
        "### 问题：规则存储在哪里？\n"
        "**面试官**：规则存储在哪里？"
    )
    assert "## 项目经历" in markdown
    assert "### 问题：为什么拆分多个 Agent？" in markdown
    assert "### 问题：规则存储在哪里？" in markdown
    assert "### 追问：" not in markdown
    assert markdown.count(ANSWER_BLOCK) == 2
    # 模块标题本身不应获得优质回答占位。
    module_part = markdown.split("### 问题：")[0]
    assert ANSWER_BLOCK not in module_part


def test_normalize_folds_legacy_follow_up_into_same_question() -> None:
    """旧的追问标题应去掉，对话仍挂在同一道主问题下。"""
    markdown = normalize_document(
        "### 问题：为什么拆分多个 Agent？\n"
        "**面试官**：为什么拆分多个 Agent？\n"
        "### 追问：不同任务如何选模型？\n"
        "**面试官**：不同任务如何选模型？"
    )
    assert "### 问题：为什么拆分多个 Agent？" in markdown
    assert "### 追问：" not in markdown
    assert "### 问题：不同任务如何选模型？" not in markdown
    assert "不同任务如何选模型？" in markdown
    assert markdown.count("**面试官**：") == 1
    assert markdown.count(ANSWER_BLOCK) == 1


def test_normalize_adds_missing_answer_block() -> None:
    """模型漏掉折叠区时，服务应补齐供用户编辑的优质回答区域。"""
    markdown = normalize_document("### 问题：项目介绍\n\n**面试官**：介绍一下项目。")
    assert markdown.startswith("### 问题：项目介绍")
    assert ANSWER_BLOCK in markdown


def test_normalize_fallback_legacy_plain_headings() -> None:
    """若模型仍输出普通 ## 标题，应回退为按二级标题挂优质回答。"""
    markdown = normalize_document("## 项目介绍\n\n**面试官**：介绍一下项目。")
    assert markdown.startswith("## 项目介绍")
    assert ANSWER_BLOCK in markdown


def test_extract_question_outline_filters_non_core_sections() -> None:
    """问题清单应只保留考点，并排除自我介绍与反问。"""
    markdown = normalize_document(
        "## 开场\n"
        "### 问题：请简单自我介绍\n"
        "**面试官**：请简单自我介绍。\n"
        "## 项目经历\n"
        "### 问题：为什么拆分多个 Agent？\n"
        "**面试官**：为什么拆分多个 Agent？\n"
        "### 问题：不同任务如何选模型？\n"
        "**面试官**：不同任务如何选模型？\n"
        "## 反问\n"
        "### 问题：团队技术栈是什么？\n"
        "**我**：团队技术栈是什么？"
    )
    outline = extract_question_outline(markdown)
    assert "1. 为什么拆分多个 Agent？" in outline
    assert "2. 不同任务如何选模型？" in outline
    assert "自我介绍" not in outline
    assert "技术栈" not in outline
    assert "优质回答" not in outline


def test_question_outline_uses_interviewer_words_for_generic_heading() -> None:
    """模型给出「问题 1」时，清单仍应显示实际题目。"""
    markdown = normalize_document(
        "### 问题 1\n"
        "**面试官**：你们如何处理 RAG 检索结果为空的情况？\n"
        "**我**：会先识别空结果，再走澄清或兜底策略。"
    )

    assert extract_question_outline(markdown) == "1. 你们如何处理 RAG 检索结果为空的情况？"


def test_question_outline_generic_headings_do_not_leak_next_question() -> None:
    """多个泛化标题时，每道题只应取自己标题下的面试官问法。"""
    markdown = normalize_document(
        "## 项目经历\n"
        "### 问题 1\n"
        "**面试官**：为什么拆分多个 Agent？\n"
        "**我**：为了让模块职责更清晰。\n"
        "### 问题 2\n"
        "**面试官**：规则存储在哪里？\n"
        "**我**：放在数据库里。"
    )

    outline = extract_question_outline(markdown)
    assert outline == (
        "1. 为什么拆分多个 Agent？\n"
        "2. 规则存储在哪里？"
    )


def test_normalize_keeps_existing_answer_block_once() -> None:
    """模型擅自写了优质回答时，应丢弃其内容并换成空占位。"""
    markdown = normalize_document(
        "### 问题：项目介绍\n\n<details>\n<summary>优质回答</summary>\n\n模型代写的内容\n\n</details>"
    )
    assert markdown.count("<summary>优质回答</summary>") == 1
    assert "模型代写的内容" not in markdown
    assert ANSWER_BLOCK in markdown
