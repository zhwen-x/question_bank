"""仓储层的共用约定。

仓储是**唯一**接触 SQLAlchemy 查询的地方。服务层只调用仓储方法，这样后续换
Postgres、或给 Web 端加分页/权限过滤，改动都收敛在这一层。

统一的软删除约定：所有读方法默认只返回未删除的行。要读回收站必须显式传
``include_deleted=True``——默认值选「不含」，是因为忘记过滤会让已删除的题
悄悄出现在组卷结果里，而反过来最多是回收站看起来是空的。
"""

from __future__ import annotations

from sqlalchemy.orm import Session


class Repo:
    """仓储基类，只持有会话。"""

    def __init__(self, session: Session) -> None:
        self.session = session


class NotFoundError(LookupError):
    """按 id 取对象但不存在。

    带上实体类型与 id，因为这个异常经常经由 CLI 或 Web 层直接显示给用户。
    """

    def __init__(self, entity: str, identifier: str) -> None:
        self.entity = entity
        self.identifier = identifier
        super().__init__(f"找不到{entity}：{identifier}")


class ConflictError(ValueError):
    """违反唯一性约束，如重名课程、同级重名章节。"""
