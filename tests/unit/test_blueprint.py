"""组卷蓝图校验测试。

蓝图是老师手写的 YAML，写错的方式比写对的方式多。这里守住的是：**写蓝图时
就报错**，而不是等组出一份 95 分的「100 分卷」才发现。
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from qbank.core.blueprint import Blueprint, SectionSpec
from qbank.core.enums import Difficulty, QuestionStatus


def _section(**overrides: object) -> SectionSpec:
    data: dict[str, object] = {"title": "一、单项选择题", "count": 10}
    data.update(overrides)
    return SectionSpec.model_validate(data)


# =================================================================== 难度配额
def test_no_mix_means_one_unconstrained_stratum() -> None:
    assert _section(count=4).quotas() == ((None, 4),)


def test_mix_is_split_by_largest_remainder() -> None:
    """四舍五入会把 0.2 那档抹成 0 道；最大余数法保证各档之和恰好等于题数。"""
    quotas = _section(count=3, difficulty_mix={1: 0.2, 2: 0.5, 3: 0.3}).quotas()
    assert sum(count for _, count in quotas) == 3
    assert quotas == (
        (Difficulty.VERY_EASY, 1),
        (Difficulty.EASY, 1),
        (Difficulty.MEDIUM, 1),
    )


@pytest.mark.parametrize("count", list(range(1, 31)))
def test_quotas_always_sum_to_count(count: int) -> None:
    """不管题数多少，分配出来的配额之和必须正好等于题数。"""
    quotas = _section(count=count, difficulty_mix={2: 0.45, 3: 0.35, 4: 0.2}).quotas()
    assert sum(number for _, number in quotas) == count


def test_quotas_are_stable_for_the_same_input() -> None:
    """完全并列的余数要按难度定序，否则同一份蓝图两次组卷的分层会不一样。"""
    spec = _section(count=5, difficulty_mix={2: 1, 3: 1, 4: 1})
    assert spec.quotas() == spec.quotas()
    assert spec.quotas() == ((Difficulty.EASY, 2), (Difficulty.MEDIUM, 2), (Difficulty.HARD, 1))


def test_zero_quota_strata_are_dropped() -> None:
    """配比里占比极小的档在题数少时会摊到 0 道，不该留下一个空单元。"""
    quotas = _section(count=2, difficulty_mix={2: 0.98, 5: 0.02}).quotas()
    assert all(number > 0 for _, number in quotas)
    assert sum(number for _, number in quotas) == 2


def test_mix_weights_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="权重必须为正数"):
        _section(difficulty_mix={2: 0.5, 3: 0})


def test_mix_and_range_are_mutually_exclusive() -> None:
    """两个字段都写时意图不明，必须让人二选一而不是替他猜。"""
    with pytest.raises(ValidationError) as excinfo:
        _section(difficulty_mix={2: 1.0}, difficulties=[3])
    message = str(excinfo.value)
    assert "difficulty_mix" in message
    assert "difficulties" in message


# ===================================================================== 总分
def test_total_score_mismatch_is_caught_while_writing_the_blueprint() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Blueprint.model_validate(
            {
                "title": "期末",
                "total_score": 100,
                "sections": [
                    {"title": "一、单选", "count": 10, "score_each": 2},
                    {"title": "二、简答", "count": 5, "score_each": 15},
                ],
            }
        )
    message = str(excinfo.value)
    assert "声明总分 100" in message
    assert "95" in message, "要把算出来的数报出来，否则人还得自己算一遍"


def test_total_score_check_is_skipped_when_scores_come_from_questions() -> None:
    """某节用题目自带分值时总分事先算不出来，此时跳过校验而不是瞎猜。"""
    blueprint = Blueprint.model_validate(
        {
            "title": "期末",
            "total_score": 100,
            "sections": [
                {"title": "一、单选", "count": 10, "score_each": 2},
                {"title": "二、综合", "count": 2},
            ],
        }
    )
    assert blueprint.sections[1].total_score is None


def test_matching_total_score_passes() -> None:
    blueprint = Blueprint.model_validate(
        {
            "title": "期末",
            "total_score": 100,
            "sections": [
                {"title": "一、单选", "count": 20, "score_each": 2},
                {"title": "二、综合", "count": 4, "score_each": 15},
            ],
        }
    )
    assert blueprint.question_count == 24
    assert blueprint.section_count == 2


# ================================================================== 默认值
def test_only_active_questions_by_default() -> None:
    """草稿题混进考卷是不可接受的，所以默认值必须是「只抽启用」。"""
    blueprint = Blueprint.model_validate({"title": "x", "sections": [{"title": "一", "count": 1}]})
    assert blueprint.statuses == (QuestionStatus.ACTIVE,)


def test_at_least_one_section() -> None:
    with pytest.raises(ValidationError):
        Blueprint.model_validate({"title": "x", "sections": []})


def test_typo_in_a_field_name_is_rejected() -> None:
    """``extra="forbid"`` 让 ``scores_each`` 这类笔误当场报错，而不是被忽略。"""
    with pytest.raises(ValidationError, match="scores_each"):
        Blueprint.model_validate(
            {"title": "x", "sections": [{"title": "一", "count": 1, "scores_each": 2}]}
        )


# ================================================================ YAML 往返
def test_yaml_round_trip() -> None:
    """蓝图是要存成文件、提交进 git、明年再用的，所以必须能原样读回来。"""
    original = Blueprint.model_validate(
        {
            "title": "2026 春季期末",
            "seed": 20261030,
            "avoid_used_within_days": 180,
            "meta": {"考试时长": "120 分钟"},
            "sections": [
                {
                    "title": "一、单项选择题",
                    "types": ["single_choice"],
                    "count": 10,
                    "score_each": 2,
                    "difficulty_mix": {2: 0.5, 3: 0.3, 4: 0.2},
                    "chapters": ["第1章 绪论"],
                    "shuffle_options": True,
                }
            ],
        }
    )
    reloaded = Blueprint.model_validate(yaml.safe_load(yaml.safe_dump(original.to_json())))
    assert reloaded == original
