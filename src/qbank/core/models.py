"""SQLAlchemy ORM 模型。

几个贯穿全局的设计决定，改动前请先理解原因：

主键用 UUID 字符串
    不用自增整数，因为需求里有「导出整库给同事导入」。自增 id 在两个库之间
    必然冲突，UUID 则天然可合并，也让将来的 ``.qbz`` 交换包里的引用保持稳定。

软删除
    ``deleted_at`` 非空即视为已删除。查询一律经仓储层，由仓储负责加上
    ``deleted_at IS NULL``。真删除只发生在「清空回收站」。

题面快照
    :class:`PaperItem` 存了成题时的 JSON 快照。去年的卷子不该因为今年改了
    题目而变样——这是教学场景的硬要求。

章节物化路径
    :class:`Chapter` 既有 ``parent_id`` 也有 ``path``。``path`` 让「某章及其
    所有子章节下的题目」变成一次 ``LIKE 'path/%'``，不必递归查询。

时间列一律用 :class:`UtcDateTime`
    SQLite 的 ``DateTime(timezone=True)`` 写进去是带时区的，读回来却是 naive
    的（实测确认）。拿它和 ``datetime.now(timezone.utc)`` 相减会直接抛
    ``TypeError``，而「避开近 180 天用过的题」正好要做这个减法。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from qbank.core.enums import Difficulty, QuestionStatus, QuestionType, TagCategory

#: 章节路径分隔符。章节标题里出现它会在写入时被替换掉。
PATH_SEP = "/"


def new_id() -> str:
    """生成主键。用带连字符的 UUID4 文本形式，便于肉眼核对与日志排查。"""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """带时区的当前时间。**不要用** ``datetime.utcnow()``——它返回 naive 时间，
    和数据库里的带时区值比较会出错。"""
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator[datetime]):
    """始终以 UTC 存取的时间列。

    - 写入：naive 值按 UTC 解释；aware 值换算到 UTC
    - 读出：补回 ``timezone.utc``，保证拿到的一定是 aware datetime

    不这么做的话，SQLite 上写 aware、读 naive，跨时区比较会抛 ``TypeError``。
    换到 Postgres 后 ``timestamptz`` 本来就正确，本装饰器是无害的恒等变换。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class StrEnumType(TypeDecorator[Any]):
    """以文本存储的枚举列。

    **为什么需要它**：把列声明成 ``mapped_column(String(20))`` 而注解写成
    ``Mapped[TagCategory]`` 时，SQLAlchemy 并不会在读出时把字符串转回枚举——
    拿到的是裸 ``str``，于是 ``tag.category.label`` 直接 AttributeError。
    靠调用方到处写 ``TagCategory(tag.category)`` 能绕过去，但那是个陷阱：
    漏一处就炸一处，而且只在运行时才发现。

    DDL 层面仍然是 ``VARCHAR(n)``，所以不影响已有迁移。
    """

    # impl 必须是**类级**变量（SQLAlchemy 在 TypeDecorator.__init__ 里断言这一点）；
    # 长度通过 super().__init__ 转交给 String 的构造函数
    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[Enum], length: int = 32) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return str(value.value)
        # 允许直接写字符串，但必须是合法成员，避免脏值进库
        return str(self.enum_class(value).value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        return None if value is None else self.enum_class(value)


class IntEnumType(TypeDecorator[Any]):
    """以整数存储的枚举列（难度）。整数便于直接参与区间筛选与分布配平。"""

    impl = Integer
    cache_ok = True

    def __init__(self, enum_class: type[Enum]) -> None:
        self.enum_class = enum_class
        super().__init__()

    def process_bind_param(self, value: Any, dialect: Dialect) -> int | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return int(value.value)
        return int(self.enum_class(value).value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        return None if value is None else self.enum_class(value)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


# ===================================================================== 课程
class Course(Base, TimestampMixin):
    __tablename__ = "course"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="course", cascade="all, delete-orphan", order_by="Chapter.sort_order"
    )
    #: ``passive_deletes=True`` 不是优化，是正确性：
    #: 删课程时若让 ORM 处理子行，它会试图把 ``question.course_id`` 置为 NULL
    #: （关系上没声明 delete 级联时的默认行为），而那一列是 NOT NULL，
    #: 于是删一门**有题目的**课程必然抛 IntegrityError。数据库这边本来就声明了
    #: ``ondelete="CASCADE"``，交给它做即可。
    questions: Mapped[list[Question]] = relationship(back_populates="course", passive_deletes=True)

    __table_args__ = (
        UniqueConstraint("name", name="uq_course_name"),
        Index("ix_course_code", "code"),
    )

    def __repr__(self) -> str:
        return f"<Course {self.name!r}>"


# ===================================================================== 章节
class Chapter(Base, TimestampMixin):
    __tablename__ = "chapter"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(
        ForeignKey("course.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False, default="", doc="如 3.2")
    #: 从根到本节的标题串联，如 ``第3章 控制结构/3.2 循环结构``。
    #: 由 ChapterRepo 维护；移动节点时必须同步重写整棵子树。
    path: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    course: Mapped[Course] = relationship(back_populates="chapters")
    parent: Mapped[Chapter | None] = relationship(
        back_populates="children", remote_side="Chapter.id"
    )
    children: Mapped[list[Chapter]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", order_by="Chapter.sort_order"
    )
    questions: Mapped[list[Question]] = relationship(back_populates="chapter")

    __table_args__ = (
        UniqueConstraint("course_id", "path", name="uq_chapter_course_path"),
        Index("ix_chapter_course_parent", "course_id", "parent_id"),
        Index("ix_chapter_path", "path"),
    )

    def __repr__(self) -> str:
        return f"<Chapter {self.path!r}>"


# ===================================================================== 标签
class Tag(Base, TimestampMixin):
    __tablename__ = "tag"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[TagCategory] = mapped_column(
        StrEnumType(TagCategory, 20), nullable=False, default=TagCategory.KNOWLEDGE
    )
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    questions: Mapped[list[Question]] = relationship(
        secondary="question_tag", back_populates="tags"
    )

    __table_args__ = (UniqueConstraint("name", name="uq_tag_name"),)

    def __repr__(self) -> str:
        return f"<Tag {self.name!r}>"


class QuestionTag(Base):
    __tablename__ = "question_tag"

    question_id: Mapped[str] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True)


# ===================================================================== 图片
class Asset(Base, TimestampMixin):
    """内容寻址的图片。

    同一张图被 100 道题引用也只存一份；``ref_count`` 让孤儿清理不必全表扫描
    Markdown 正文。
    """

    __tablename__ = "asset"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    suffix: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("sha256", name="uq_asset_sha256"),
        CheckConstraint("ref_count >= 0", name="ck_asset_ref_count_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<Asset {self.sha256[:12]} {self.mime}>"


class QuestionAsset(Base):
    __tablename__ = "question_asset"

    question_id: Mapped[str] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("asset.id", ondelete="CASCADE"), primary_key=True
    )


# ===================================================================== 题目
class Question(Base, TimestampMixin):
    __tablename__ = "question"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(
        ForeignKey("course.id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter.id", ondelete="SET NULL"), nullable=True
    )
    #: 综合应用题的子题指向父题；父题自身 parent_id 为空
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    type: Mapped[QuestionType] = mapped_column(StrEnumType(QuestionType, 32), nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    difficulty: Mapped[Difficulty] = mapped_column(
        IntEnumType(Difficulty), nullable=False, default=Difficulty.MEDIUM
    )
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    est_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[QuestionStatus] = mapped_column(
        StrEnumType(QuestionStatus, 16), nullable=False, default=QuestionStatus.DRAFT
    )
    source: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    author: Mapped[str] = mapped_column(String(100), nullable=False, default="")

    #: 规范化题面的精确指纹，用于「完全重复」判定。
    #: 近似重复**不存指纹**——实测任何表面相似度阈值都分不开「换个数字的同一道题」
    #: 和「正确↔错误的不同题」，所以相似候选改为查询时在缩小后的候选集上实时
    #: 计算（见 qbank.core.services.dedup）。
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 来源文件里的稳定标识，让同一文件重复导入能认出是同一道题
    external_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    course: Mapped[Course] = relationship(back_populates="questions")
    chapter: Mapped[Chapter | None] = relationship(back_populates="questions")
    parent: Mapped[Question | None] = relationship(
        back_populates="sub_questions", remote_side="Question.id"
    )
    sub_questions: Mapped[list[Question]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", order_by="Question.sort_order"
    )
    answer: Mapped[QuestionAnswer | None] = relationship(
        back_populates="question", cascade="all, delete-orphan", uselist=False
    )
    tags: Mapped[list[Tag]] = relationship(secondary="question_tag", back_populates="questions")
    assets: Mapped[list[Asset]] = relationship(secondary="question_asset")
    revisions: Mapped[list[QuestionRevision]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="QuestionRevision.revision",
    )

    __table_args__ = (
        # 覆盖「列表页筛选」与「自动组卷抽题」两个最热的查询
        Index("ix_question_browse", "course_id", "deleted_at", "status", "type", "difficulty"),
        Index("ix_question_chapter", "chapter_id", "deleted_at"),
        Index("ix_question_parent", "parent_id", "sort_order"),
        Index("ix_question_fingerprint", "fingerprint"),
        Index("ix_question_external", "course_id", "external_id"),
        CheckConstraint("score >= 0", name="ck_question_score_non_negative"),
        CheckConstraint("difficulty BETWEEN 1 AND 5", name="ck_question_difficulty_range"),
        CheckConstraint("id <> parent_id", name="ck_question_not_own_parent"),
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_sub_question(self) -> bool:
        return self.parent_id is not None

    def __repr__(self) -> str:
        head = self.stem[:24].replace("\n", " ")
        return f"<Question {self.type} {head!r}>"


class QuestionAnswer(Base, TimestampMixin):
    """答案与解析单独一张表。

    拆开的理由：题目列表页只需要题干，不该把动辄几千字的解析一起读出来。
    """

    __tablename__ = "question_answer"

    question_id: Mapped[str] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), primary_key=True
    )
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scoring_rubric: Mapped[str] = mapped_column(Text, nullable=False, default="")

    question: Mapped[Question] = relationship(back_populates="answer")


class QuestionRevision(Base):
    """每次编辑**前**的整题快照，支持回滚。

    ``revision`` 是快照所代表的版本号（即改动前的版本），``note`` 描述的是
    紧随其后的那次改动。于是界面上可以直接读成「恢复到『改分值』之前的状态」
    ——这正是用户想回滚时的思考方式。
    """

    __tablename__ = "question_revision"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    question_id: Mapped[str] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    changed_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, server_default=func.now(), nullable=False
    )

    question: Mapped[Question] = relationship(back_populates="revisions")

    __table_args__ = (UniqueConstraint("question_id", "revision", name="uq_revision_question_rev"),)


# ===================================================================== 试卷
class Paper(Base, TimestampMixin):
    __tablename__ = "paper"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(
        ForeignKey("course.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    #: 考试名称、时长、班级、日期、命题人…… 导出模板直接读这里
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    template_key: Mapped[str] = mapped_column(String(100), nullable=False, default="standard")
    #: 自动组卷的随机种子；同种子 + 同蓝图可复现同一份卷子
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 生成本卷所用的蓝图，留档以便「照着去年的规则再出一份」
    blueprint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    course: Mapped[Course] = relationship()
    sections: Mapped[list[PaperSection]] = relationship(
        back_populates="paper", cascade="all, delete-orphan", order_by="PaperSection.sort_order"
    )

    __table_args__ = (Index("ix_paper_course", "course_id", "deleted_at"),)

    @property
    def total_score(self) -> float:
        return sum(item.score for section in self.sections for item in section.items)

    def __repr__(self) -> str:
        return f"<Paper {self.title!r}>"


class PaperSection(Base):
    """试卷的一个大题，如「一、单项选择题（每题 2 分，共 20 分）」。"""

    __tablename__ = "paper_section"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("paper.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    paper: Mapped[Paper] = relationship(back_populates="sections")
    items: Mapped[list[PaperItem]] = relationship(
        back_populates="section", cascade="all, delete-orphan", order_by="PaperItem.sort_order"
    )

    __table_args__ = (Index("ix_paper_section_paper", "paper_id", "sort_order"),)

    @property
    def total_score(self) -> float:
        return sum(item.score for item in self.items)


class PaperItem(Base):
    """试卷上的一道题。

    ``snapshot`` 冻结了成卷那一刻的完整题面。``question_id`` 保留用于统计与
    「跳到原题」，但**渲染一律用 snapshot**，这样原题被改或被删都不影响旧卷。
    """

    __tablename__ = "paper_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("paper_section.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str | None] = mapped_column(
        ForeignKey("question.id", ondelete="SET NULL"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    #: 本题选项的展示顺序（A/B 卷乱序用）。空表示按原序。
    option_order: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    section: Mapped[PaperSection] = relationship(back_populates="items")
    question: Mapped[Question | None] = relationship()

    __table_args__ = (
        Index("ix_paper_item_section", "section_id", "sort_order"),
        Index("ix_paper_item_question", "question_id"),
        CheckConstraint("score >= 0", name="ck_paper_item_score_non_negative"),
    )


class QuestionUsage(Base):
    """题目被用进哪份卷子。自动组卷据此避开近期已考过的题。"""

    __tablename__ = "question_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    question_id: Mapped[str] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("paper.id", ondelete="CASCADE"), nullable=False
    )
    used_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("question_id", "paper_id", name="uq_usage_question_paper"),
        Index("ix_usage_question_time", "question_id", "used_at"),
    )


# ===================================================================== 导入
class ImportBatch(Base):
    """一次导入的记录。失败可重试，成功可回滚。"""

    __tablename__ = "import_batch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    course_id: Mapped[str | None] = mapped_column(
        ForeignKey("course.id", ondelete="SET NULL"), nullable=True
    )
    source_name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    source_format: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    #: 源文件内容哈希，用于识别「这个文件已经导过了」
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 逐条结果与错误信息，供导入向导回显
    report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: 本批新建的题目 id，回滚时据此删除
    created_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (Index("ix_import_batch_time", "started_at"),)
