"""表单 → :class:`QuestionDraft`。

**为什么不直接把表单丢给 Pydantic**：题型不同，payload 的字段完全不同
（选择题是选项列表，填空题是每空答案，综合题是子题块）。表单层要先按题型把
扁平的 ``option_label[]``、``blank_answer[]`` 这类重复字段组装成结构，
再交给 Pydantic 校验。

**校验失败必须保留用户已填的内容**。老师写一道综合题可能花十分钟，因为漏填
一个分值就把整页清空，是不可接受的。所以本模块抛 :class:`FormError`，里面
带着「哪个字段错了」以及原始表单数据，由模板回填。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import ValidationError
from starlette.datastructures import FormData

from qbank.core.blueprint import Blueprint
from qbank.core.enums import (
    BlankMatch,
    Difficulty,
    QuestionStatus,
    QuestionType,
    parse_question_type,
)
from qbank.core.schemas import QuestionDraft

#: 子题最多几个。防的是表单被恶意或误操作塞进上万行。
MAX_SUB_QUESTIONS = 30

#: 选项最多几个
MAX_OPTIONS = 26

#: 一份蓝图最多几个大题
MAX_SECTIONS = 20


@dataclass
class FormError(Exception):
    """表单校验失败。

    ``messages`` 是页面顶部的汇总，``fields`` 是逐字段的内联提示。
    两者都给，因为有些错误（如「选项标号重复」）没法归到单一字段上。
    """

    messages: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)

    def add(self, message: str, *, field_name: str = "") -> None:
        self.messages.append(message)
        if field_name:
            self.fields.setdefault(field_name, message)

    @property
    def is_empty(self) -> bool:
        return not self.messages and not self.fields

    def __str__(self) -> str:
        return "；".join(self.messages) if self.messages else "表单校验失败"


def _text(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else default


def _texts(form: FormData, key: str) -> list[str]:
    return [value for value in form.getlist(key) if isinstance(value, str)]


def _checked(form: FormData, key: str) -> bool:
    """复选框：未勾选时浏览器根本不提交这个字段。"""
    return form.get(key) is not None


def _number(form: FormData, key: str, *, default: float = 0.0) -> float:
    raw = _text(form, key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _split_tags(raw: str) -> tuple[str, ...]:
    """标签输入框：逗号、顿号、分号、空格都当分隔符，并去重。

    >>> _split_tags("循环, 边界条件、while")
    ('循环', '边界条件', 'while')
    >>> _split_tags("循环 循环, 循环")
    ('循环',)
    >>> _split_tags("  ")
    ()
    """
    seen: dict[str, None] = {}
    for piece in re.split(r"[,，、;；\s]+", raw):
        cleaned = piece.strip().strip("#")
        if cleaned:
            seen[cleaned] = None
    return tuple(seen)


def _parse_options(form: FormData, kind: QuestionType, errors: FormError) -> dict[str, Any]:
    """组装选择题 payload。

    正确项来自 ``correct`` 字段（单选是 radio、多选是 checkbox），值是选项标号。
    不像导入那样需要从文本里猜——界面上点选是明确的。
    """
    labels = _texts(form, "option_label")
    contents = _texts(form, "option_content")
    correct = {value.upper() for value in _texts(form, "correct")}

    rows: list[dict[str, Any]] = []
    for index, (label, content) in enumerate(zip(labels, contents, strict=False)):
        label = label.strip().upper()
        content = content.strip()
        if not label and not content:
            continue  # 空行是用户点了「添加选项」又没填，直接忽略
        if not label:
            errors.add(f"第 {index + 1} 个选项缺少标号", field_name=f"option_label.{index}")
            continue
        if not content:
            errors.add(f"选项 {label} 没有内容", field_name=f"option_content.{index}")
            continue
        rows.append({"label": label, "content": content, "is_correct": label in correct})

    if len(rows) > MAX_OPTIONS:
        errors.add(f"选项不得超过 {MAX_OPTIONS} 个")
        rows = rows[:MAX_OPTIONS]
    if len(rows) < 2:
        errors.add("选择题至少需要 2 个选项", field_name="options")
    seen_labels = [row["label"] for row in rows]
    duplicates = {label for label in seen_labels if seen_labels.count(label) > 1}
    if duplicates:
        errors.add(f"选项标号重复：{'、'.join(sorted(duplicates))}", field_name="options")

    marked = [row["label"] for row in rows if row["is_correct"]]
    if not marked:
        errors.add("请勾选正确选项", field_name="correct")
    elif kind is QuestionType.SINGLE_CHOICE and len(marked) > 1:
        errors.add("单选题只能有一个正确选项", field_name="correct")

    return {
        "type": kind.value,
        "options": rows,
        "shuffle_ok": _checked(form, "shuffle_ok"),
    }


def _parse_blanks(form: FormData, errors: FormError) -> dict[str, Any]:
    """组装填空题 payload。每空一行，备选答案用 ``/`` 分隔。"""
    answers = _texts(form, "blank_answer")
    matches = _texts(form, "blank_match")
    tolerances = _texts(form, "blank_tolerance")

    blanks: list[dict[str, Any]] = []
    for index, raw in enumerate(answers):
        text = raw.strip()
        if not text:
            continue
        alternatives = [part.strip() for part in text.split("/") if part.strip()]
        mode = matches[index] if index < len(matches) else BlankMatch.EXACT.value
        tolerance = 0.0
        if mode == BlankMatch.NUMERIC.value and index < len(tolerances):
            try:
                tolerance = float(tolerances[index] or 0)
            except ValueError:
                errors.add(
                    f"第 {len(blanks) + 1} 空的允许误差不是数字",
                    field_name=f"blank_tolerance.{index}",
                )
        blanks.append(
            {
                "index": len(blanks) + 1,
                "answers": alternatives or [text],
                "match": mode,
                "tolerance": tolerance,
            }
        )
    if not blanks:
        errors.add("填空题至少要填一个空的答案", field_name="blank_answer")
    return {"type": QuestionType.FILL_BLANK.value, "blanks": blanks}


def _parse_programming(form: FormData) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    names = _texts(form, "tc_name")
    inputs = _texts(form, "tc_input")
    expected = _texts(form, "tc_expected")
    hidden = set(_texts(form, "tc_hidden"))
    for index in range(max(len(names), len(inputs), len(expected))):
        case_input = inputs[index] if index < len(inputs) else ""
        case_expected = expected[index] if index < len(expected) else ""
        if not case_input.strip() and not case_expected.strip():
            continue
        cases.append(
            {
                "name": names[index].strip() if index < len(names) else "",
                "input": case_input,
                "expected": case_expected,
                "hidden": str(index) in hidden,
            }
        )
    return {
        "type": QuestionType.PROGRAMMING.value,
        "language": _text(form, "language"),
        "function_signature": _text(form, "signature"),
        "starter_code": _text(form, "starter_code"),
        "test_cases": cases,
        "time_limit_ms": int(_number(form, "time_limit_ms", default=1000)) or 1000,
        "memory_limit_mb": int(_number(form, "memory_limit_mb", default=256)) or 256,
    }


def _parse_sub_questions(form: FormData, errors: FormError) -> list[dict[str, Any]]:
    """组装综合题的子题。"""
    types = _texts(form, "sub_type")
    stems = _texts(form, "sub_stem")
    answers = _texts(form, "sub_answer")
    explanations = _texts(form, "sub_explanation")
    scores = _texts(form, "sub_score")

    subs: list[dict[str, Any]] = []
    for index, stem in enumerate(stems):
        if not stem.strip():
            continue
        raw_type = types[index] if index < len(types) else QuestionType.SHORT_ANSWER.value
        try:
            sub_type = parse_question_type(raw_type)
        except ValueError:
            sub_type = QuestionType.SHORT_ANSWER
        if sub_type in {
            QuestionType.SINGLE_CHOICE,
            QuestionType.MULTIPLE_CHOICE,
            QuestionType.COMPREHENSIVE,
        }:
            # 子题里再放选择题需要嵌套的选项编辑器，界面上收益很低；
            # 综合题套综合题则是明确不支持的（只支持两层）
            errors.add(
                f"第 {len(subs) + 1} 个小问不支持「{sub_type.label}」，请用简答、填空、判断或编程",
                field_name=f"sub_type.{index}",
            )
            continue
        try:
            score = float(scores[index]) if index < len(scores) and scores[index] else 0.0
        except ValueError:
            score = 0.0
            errors.add(f"第 {len(subs) + 1} 个小问的分值不是数字", field_name=f"sub_score.{index}")
        payload: dict[str, Any] = {"type": sub_type.value}
        if sub_type is QuestionType.TRUE_FALSE:
            payload["correct"] = (
                answers[index].strip().startswith(("对", "正", "T", "t", "是", "√"))
                if index < len(answers)
                else True
            )
        elif sub_type is QuestionType.FILL_BLANK:
            text = answers[index].strip() if index < len(answers) else ""
            pieces = [part.strip() for part in text.split("；") if part.strip()] or [text or "—"]
            payload["blanks"] = [
                {"index": position, "answers": [piece]}
                for position, piece in enumerate(pieces, start=1)
            ]
        subs.append(
            {
                "type": sub_type.value,
                "stem": stem.strip(),
                "payload": payload,
                "answer": answers[index].strip() if index < len(answers) else "",
                "explanation": (explanations[index].strip() if index < len(explanations) else ""),
                "score": score,
            }
        )
    if len(subs) > MAX_SUB_QUESTIONS:
        errors.add(f"小问不得超过 {MAX_SUB_QUESTIONS} 个")
        subs = subs[:MAX_SUB_QUESTIONS]
    if not subs:
        errors.add("综合应用题至少要有一个小问", field_name="sub_stem")
    return subs


def parse_question_form(form: FormData) -> QuestionDraft:
    """表单 → 草稿。校验不通过时抛 :class:`FormError`。"""
    errors = FormError()

    raw_type = _text(form, "type")
    try:
        kind = parse_question_type(raw_type) if raw_type else QuestionType.SINGLE_CHOICE
    except ValueError as exc:
        errors.add(str(exc), field_name="type")
        raise errors from exc

    stem = _text(form, "stem")
    if not stem:
        errors.add("题干不能为空", field_name="stem")

    payload: dict[str, Any]
    if kind in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
        payload = _parse_options(form, kind, errors)
    elif kind is QuestionType.TRUE_FALSE:
        payload = {"type": kind.value, "correct": _text(form, "tf_correct") == "true"}
    elif kind is QuestionType.FILL_BLANK:
        payload = _parse_blanks(form, errors)
    elif kind is QuestionType.PROGRAMMING:
        payload = _parse_programming(form)
    elif kind is QuestionType.COMPREHENSIVE:
        payload = {"type": kind.value, "material_label": _text(form, "material_label")}
    else:
        points = tuple(
            line.strip() for line in _text(form, "key_points").split("\n") if line.strip()
        )
        payload = {"type": kind.value, "key_points": points}

    sub_questions = _parse_sub_questions(form, errors) if kind is QuestionType.COMPREHENSIVE else []

    difficulty_value = int(_number(form, "difficulty", default=3)) or 3
    if difficulty_value not in {level.value for level in Difficulty}:
        errors.add("难度必须是 1 到 5", field_name="difficulty")
        difficulty_value = 3

    status_value = _text(form, "status") or QuestionStatus.DRAFT.value
    if status_value not in {state.value for state in QuestionStatus}:
        status_value = QuestionStatus.DRAFT.value

    if not errors.is_empty:
        raise errors

    try:
        return QuestionDraft.model_validate(
            {
                "type": kind.value,
                "stem": stem,
                "payload": payload,
                "answer": _text(form, "answer"),
                "explanation": _text(form, "explanation"),
                "scoring_rubric": _text(form, "scoring_rubric"),
                "chapter": _text(form, "chapter"),
                "difficulty": difficulty_value,
                "score": _number(form, "score"),
                "est_minutes": int(_number(form, "est_minutes")),
                "tags": _split_tags(_text(form, "tags")),
                "source": _text(form, "source"),
                "status": status_value,
                "external_id": _text(form, "external_id"),
                "sub_questions": sub_questions,
            }
        )
    except ValueError as exc:
        # Pydantic 的原始报错对用户太啰嗦，取「Value error, 」之后那段真正的原因
        detail = str(exc).split("Value error, ")[-1].split(" [type=")[0].strip()
        errors.add(detail)
        raise errors from exc


# ================================================================ 组卷蓝图
#: 难度表达式里的一项：``3`` / ``3-5`` / ``2:0.3`` / ``2:30%``
_DIFFICULTY_ITEM = re.compile(
    r"^\s*(?P<from>[1-5])\s*(?:-\s*(?P<to>[1-5])\s*)?(?::\s*(?P<weight>[0-9.]+)\s*%?\s*)?$"
)

#: 难度表达式的分隔符：中英文逗号、顿号、分号、空格
_DIFFICULTY_SPLIT = re.compile(r"[,，、;；\s]+")


def parse_difficulty_spec(raw: str) -> tuple[dict[Difficulty, float], tuple[Difficulty, ...]]:
    """解析一个大题的难度要求，返回 ``(配比, 范围)``。

    一个输入框承担两种语义，因为老师的需求本来就是两种：「易 3 中 5 难 2」是
    配比，「中等及以上」只是范围。带 ``:`` 的当配比，不带的当范围——这样两种
    都能一行写完，不必先在界面上选一个模式。

    >>> parse_difficulty_spec("")
    ({}, ())
    >>> parse_difficulty_spec("3-5")[1]
    (<Difficulty.MEDIUM: 3>, <Difficulty.HARD: 4>, <Difficulty.VERY_HARD: 5>)
    >>> parse_difficulty_spec("3、4,5")[1]
    (<Difficulty.MEDIUM: 3>, <Difficulty.HARD: 4>, <Difficulty.VERY_HARD: 5>)
    >>> parse_difficulty_spec("2:3 3:5 4:2")[0]
    {<Difficulty.EASY: 2>: 3.0, <Difficulty.MEDIUM: 3>: 5.0, <Difficulty.HARD: 4>: 2.0}
    >>> parse_difficulty_spec("2:30%, 3:70%")[0]
    {<Difficulty.EASY: 2>: 30.0, <Difficulty.MEDIUM: 3>: 70.0}
    """
    text = raw.strip()
    if not text:
        return {}, ()
    mix: dict[Difficulty, float] = {}
    levels: list[Difficulty] = []
    for piece in _DIFFICULTY_SPLIT.split(text):
        if not piece:
            continue
        matched = _DIFFICULTY_ITEM.match(piece)
        if matched is None:
            raise ValueError(
                f"难度「{piece}」看不懂。可以写 `3`、`3-5`、`3,4,5`（只限范围），"
                "或 `2:3 3:5 4:2`、`2:30% 3:70%`（按配比抽）"
            )
        weight = matched.group("weight")
        start = int(matched.group("from"))
        end = int(matched.group("to") or start)
        if weight is not None:
            if end != start:
                raise ValueError(f"难度「{piece}」：配比要逐档写，不能给一个区间配权重")
            mix[Difficulty(start)] = float(weight)
        else:
            levels.extend(
                Difficulty(value) for value in range(min(start, end), max(start, end) + 1)
            )
    if mix and levels:
        raise ValueError(
            "难度里混了配比和范围两种写法。要配比就每档都带 `:权重`（如 `2:3 3:5`），"
            "只限范围就都不带（如 `3-5`）"
        )
    ordered = tuple(sorted(set(levels), key=lambda level: level.value))
    return mix, ordered


def section_indices(form: FormData) -> list[int]:
    """表单里出现过的大题下标，按序返回。

    前端删掉中间一行时留下的空号是允许的——按下标排序即可保持顺序，省掉一段
    「删一行就把后面全部重新编号」的 JS（那段 JS 一旦出错，用户会看到两行同名
    字段互相覆盖，而且很难察觉）。
    """
    found: set[int] = set()
    for key in form:
        if key.startswith("section_title_"):
            suffix = key[len("section_title_") :]
            if suffix.isdigit():
                found.add(int(suffix))
    return sorted(found)


def parse_blueprint_form(form: FormData) -> Blueprint:
    """表单 → :class:`Blueprint`。

    校验错误一律收进 :class:`FormError`，由模板回填——组卷蓝图有十几个字段，
    填错一个就清空整页是不可接受的。
    """
    error = FormError()
    title = _text(form, "title")
    if not title:
        error.add("试卷标题不能为空", field_name="title")

    indices = section_indices(form)
    if not indices:
        error.add("至少要有一个大题")
    if len(indices) > MAX_SECTIONS:
        error.add(f"大题不能超过 {MAX_SECTIONS} 个")
        indices = indices[:MAX_SECTIONS]

    sections: list[dict[str, Any]] = []
    for index in indices:
        section_title = _text(form, f"section_title_{index}")
        if not section_title:
            error.add(f"第 {len(sections) + 1} 个大题没有标题", field_name=f"section_title_{index}")
            continue
        spec: dict[str, Any] = {
            "title": section_title,
            "instructions": _text(form, f"section_instructions_{index}"),
            "count": int(_number(form, f"section_count_{index}", default=0)),
            "include_unchaptered": _checked(form, f"section_unchaptered_{index}"),
            "chapters": tuple(_texts(form, f"section_chapter_{index}")),
            "tags_any": tuple(_texts(form, f"section_tag_{index}")),
            "require_answer": _checked(form, f"section_require_answer_{index}"),
            "shuffle_options": _checked(form, f"section_shuffle_{index}"),
        }
        kind = _text(form, f"section_type_{index}")
        if kind:
            spec["types"] = (kind,)
        score_each = _text(form, f"section_score_{index}")
        if score_each:
            spec["score_each"] = _number(form, f"section_score_{index}")
        try:
            mix, levels = parse_difficulty_spec(_text(form, f"section_difficulty_{index}"))
        except ValueError as exc:
            error.add(f"{section_title}：{exc}", field_name=f"section_difficulty_{index}")
        else:
            if mix:
                spec["difficulty_mix"] = mix
            if levels:
                spec["difficulties"] = levels
        sections.append(spec)

    data: dict[str, Any] = {
        "title": title,
        "sections": sections,
        "shuffle_items": _checked(form, "shuffle_items"),
        "statuses": (
            (QuestionStatus.ACTIVE, QuestionStatus.DRAFT)
            if _checked(form, "include_draft")
            else (QuestionStatus.ACTIVE,)
        ),
    }
    if _text(form, "total_score"):
        data["total_score"] = _number(form, "total_score")
    if _text(form, "seed"):
        data["seed"] = int(_number(form, "seed"))
    if _text(form, "avoid_days"):
        data["avoid_used_within_days"] = int(_number(form, "avoid_days"))
    meta = {
        key: value
        for key, value in (
            ("考试时长", _text(form, "meta_duration")),
            ("适用班级", _text(form, "meta_class")),
            ("命题人", _text(form, "meta_author")),
        )
        if value
    }
    if meta:
        data["meta"] = meta

    if not error.is_empty:
        raise error
    try:
        return Blueprint.model_validate(data)
    except ValidationError as exc:
        for item in exc.errors():
            location = ".".join(str(part) for part in item["loc"])
            error.add(f"{location or '蓝图'}：{item['msg']}")
        raise error from exc


def parse_blueprint_yaml(raw: str) -> Blueprint:
    """粘贴的蓝图 YAML → :class:`Blueprint`。

    结构化表单每节只能选一个题型（放七个复选框会把那一行撑爆），而
    ``types: [comprehensive, programming]`` 这类写法是真实需求。留这条路进来，
    界面就不比命令行少功能，也不必为少数场景把表单做成一棵树。
    """
    error = FormError()
    if not raw.strip():
        error.add("蓝图内容是空的")
        raise error
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        error.add(f"YAML 语法错误：{exc}")
        raise error from exc
    if not isinstance(data, dict):
        error.add("蓝图的最外层应该是一组 `键: 值`，而不是列表或单个值")
        raise error
    try:
        return Blueprint.model_validate(data)
    except ValidationError as exc:
        paths = [tuple(str(part) for part in item["loc"]) for item in exc.errors()]
        for item, here in zip(exc.errors(), paths, strict=True):
            # 丢掉上层的连带报错：某节里写错字段名时，pydantic 会再报一句
            # 「sections 至少要有 1 项」，那句会把人引向错误的方向
            if any(other != here and other[: len(here)] == here for other in paths):
                continue
            error.add(f"{'.'.join(here) or '蓝图'}：{item['msg']}")
        raise error from exc
