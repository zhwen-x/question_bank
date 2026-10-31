"""组卷蓝图：一份「怎么出卷」的可复用说明书。

蓝图和试卷是两回事。蓝图是**规则**（各大题要什么题型、几道、难度怎么配），
试卷是规则的**一次执行结果**。把规则单独存下来，才能做到「照着去年的规则再
出一份」和「同一份规则出 A/B 两套卷」——这两件事都是真实的教学需求。

难度为什么是两个字段
    老师的需求实际上有三种：「10 道单选，其中易 2、中 5、难 3」（配比）、
    「10 道单选，中等及以上」（范围）、「10 道单选，难度不限」。只提供配比
    字段的话，第二种需求就得被迫写成一个他自己也不想要的比例，于是蓝图说的
    和老师想的不是一回事。所以 ``difficulty_mix`` 与 ``difficulties`` 分开，
    且互斥。

章节和标签为什么用名字而不是 id
    蓝图是要**跨题库复用**的文件（发给同事、存进 git、明年再用一次）。写 id
    的话换一个库就全部失效。代价是名字写错时必须报错而不是悄悄抽不到题——
    见 :mod:`qbank.core.services.selection` 里的解析逻辑。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType

#: 单节题量上限。够大到不碍事，又能挡住 `count: 100000` 这种手滑。
MAX_SECTION_COUNT = 500


class _BlueprintBase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class SectionSpec(_BlueprintBase):
    """蓝图里的一个大题，如「一、单项选择题（每题 2 分，共 20 分）」。"""

    title: str = Field(min_length=1, max_length=200, description="大题标题")
    instructions: str = Field(default="", description="答题说明，印在大题标题下")
    types: tuple[QuestionType, ...] = Field(default=(), description="限定题型；留空表示不限")
    count: int = Field(ge=1, le=MAX_SECTION_COUNT, description="本节题数")
    score_each: float | None = Field(
        default=None, ge=0, le=1000, description="每题分值；留空表示用题目自带的分值"
    )
    chapters: tuple[str, ...] = Field(
        default=(), description="限定章节路径（含其全部子章节）；留空表示不限"
    )
    include_unchaptered: bool = Field(default=False, description="是否也抽未归类的题")
    difficulty_mix: dict[Difficulty, float] = Field(
        default_factory=dict, description="难度配比，如 {2: 0.5, 3: 0.3, 4: 0.2}；按比例分层抽样"
    )
    difficulties: tuple[Difficulty, ...] = Field(
        default=(), description="只限定难度范围、不管分布；与 difficulty_mix 互斥"
    )
    tags_all: tuple[str, ...] = Field(default=(), description="必须同时具备的标签")
    tags_any: tuple[str, ...] = Field(default=(), description="具备任一即可的标签")
    source_contains: str = Field(default="", description="来源包含该字样，如「2025 期末」")
    require_answer: bool = Field(default=False, description="只抽有参考答案的题")
    shuffle_options: bool = Field(default=False, description="打乱选项顺序（出 A/B 卷用）")

    @field_validator("difficulty_mix")
    @classmethod
    def _check_mix(cls, mix: dict[Difficulty, float]) -> dict[Difficulty, float]:
        if not mix:
            return mix
        bad = sorted(level.value for level, weight in mix.items() if weight <= 0)
        if bad:
            raise ValueError(f"难度配比的权重必须为正数，难度 {bad} 不满足；不要的难度直接别写")
        return mix

    @model_validator(mode="after")
    def _mix_or_range_not_both(self) -> SectionSpec:
        if self.difficulty_mix and self.difficulties:
            raise ValueError(
                f"「{self.title}」同时写了 difficulty_mix 和 difficulties。"
                "配比用 difficulty_mix（如 {2: 0.5, 3: 0.5}），"
                "只限范围用 difficulties（如 [3, 4, 5]），二者只能有一个"
            )
        return self

    @property
    def total_score(self) -> float | None:
        """本节总分。``score_each`` 为空时算不出来（要等抽到题才知道）。"""
        return None if self.score_each is None else self.score_each * self.count

    def quotas(self) -> tuple[tuple[Difficulty | None, int], ...]:
        """把难度配比摊成整数配额，各档之和**恰好**等于 ``count``。

        用最大余数法分配，而不是四舍五入后凑数——四舍五入会出现「配比
        {1: 0.2, 2: 0.5, 3: 0.3} 要 3 道题，算出来 0+2+1=3 道」这类看起来对、
        实则把 0.2 那档抹成 0 道的结果。排序键里带上难度值，保证同样的输入
        每次给出同样的分配（可复现是组卷的硬要求）。

        >>> spec = SectionSpec(title="一、单选", count=10, difficulty_mix={2: 0.5, 3: 0.5})
        >>> spec.quotas()
        ((<Difficulty.EASY: 2>, 5), (<Difficulty.MEDIUM: 3>, 5))
        >>> SectionSpec(title="二、填空", count=4).quotas()
        ((None, 4),)
        """
        if not self.difficulty_mix:
            return ((None, self.count),)
        total_weight = sum(self.difficulty_mix.values())
        raw = {
            level: weight / total_weight * self.count
            for level, weight in sorted(self.difficulty_mix.items())
        }
        quota = {level: int(value) for level, value in raw.items()}
        remaining = self.count - sum(quota.values())
        # 余数大的先补；完全并列时按难度从低到高，保证结果可复现
        ranked = sorted(raw, key=lambda level: (-(raw[level] - quota[level]), level.value))
        for level in ranked[:remaining]:
            quota[level] += 1
        return tuple((level, count) for level, count in sorted(quota.items()) if count)


class Blueprint(_BlueprintBase):
    """一份完整的组卷规则。

    ``seed`` 决定抽题的随机性。同一个库 + 同一份蓝图 + 同一个 seed，抽出来的
    题目与顺序完全一致——这既是「复现去年那份卷」的依据，也是出 A/B 卷时
    「只改 seed」的依据。
    """

    title: str = Field(min_length=1, max_length=300, description="试卷标题")
    course: str = Field(default="", description="课程名；命令行用，Web 端由当前课程决定")
    total_score: float | None = Field(
        default=None, ge=0, description="期望总分；填了就会校验各节配分之和是否对得上"
    )
    seed: int | None = Field(default=None, description="随机种子；留空则每次组卷现取一个")
    avoid_used_within_days: int | None = Field(
        default=None, ge=0, description="避开最近 N 天用过的题；留空表示不避"
    )
    statuses: tuple[QuestionStatus, ...] = Field(
        default=(QuestionStatus.ACTIVE,),
        description="只抽这些状态的题。默认只抽「启用」，草稿与停用的不进卷",
    )
    shuffle_items: bool = Field(default=False, description="打乱每节内部的题目顺序")
    template_key: str = Field(default="standard", description="导出模板标识")
    meta: dict[str, str] = Field(
        default_factory=dict, description="考试名称、时长、班级、命题人…… 导出模板直接读这里"
    )
    sections: tuple[SectionSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _total_score_matches(self) -> Blueprint:
        """声明的总分要和各节配分之和对得上。

        在这里拦住，而不是等组卷出来才发现——「我想出 100 分卷，结果配成了
        95 分」属于写蓝图时的笔误，越早报越省事。各节里只要有一节没写
        ``score_each``（表示用题目自带分值），总分就无法事先算出，此时跳过
        校验而不是瞎猜。
        """
        if self.total_score is None:
            return self
        per_section = [section.total_score for section in self.sections]
        if any(value is None for value in per_section):
            return self
        computed = sum(value for value in per_section if value is not None)
        if abs(computed - self.total_score) > 1e-6:
            detail = "、".join(
                f"{section.title} {section.score_each}×{section.count}" for section in self.sections
            )
            raise ValueError(
                f"声明总分 {self.total_score:g}，但各节配分之和是 {computed:g}（{detail}）"
            )
        return self

    @property
    def section_count(self) -> int:
        return len(self.sections)

    @property
    def question_count(self) -> int:
        return sum(section.count for section in self.sections)

    def to_json(self) -> dict[str, Any]:
        """存进 ``paper.blueprint`` 的形式，留档以便照原规则重出一份。"""
        return self.model_dump(mode="json")
