"""生成适配器公共契约与输出校验。

所有适配器都返回同一份结构：
  {"code": str, "adapter": str, "ai_generated": bool, "usage": {...}, "self_check": [...]}

输出校验（validate）是生成侧的第一道自检，和闸门是两件事：
- 这里只查"生成的代码是否满足硬性契约"（事务注解、提交/回滚、定点数）；
- 闸门查"行为的等价性"（编译 / 契约测试 / 双跑差分 / AST）。
自检失败会给模型一次修复机会，修复不了就交给闸门兜底。
"""
from __future__ import annotations

import hashlib
import re

CLASS_NAME = "ModernSettleService"


class AdapterError(RuntimeError):
    pass


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def strip_fences(text: str) -> str:
    fence = re.search(r"```(?:java)?\s*\n(.*?)```", text, re.S)
    return fence.group(1).strip() if fence else text.strip()


def validate(code: str) -> list:
    """返回问题列表；空列表代表通过自检。"""
    problems = []
    if "```" in code:
        problems.append("输出仍包含 Markdown 代码围栏")
    if "class {} ".format(CLASS_NAME) not in code and "class {}<".format(CLASS_NAME) not in code:
        problems.append("未声明 class {}".format(CLASS_NAME))
    if "implements SettleApi" not in code:
        problems.append("未实现 SettleApi 接口")
    if not re.search(r"@TxRequired\b", code):
        problems.append("缺少 @TxRequired 事务声明（MAP-001 / RULE-021）")
    if "dao.commit()" not in code:
        problems.append("缺少 dao.commit()，事务不会提交（MAP-001）")
    if "dao.rollback()" not in code:
        problems.append("缺少 dao.rollback()，异常路径无法回滚（MAP-001）")
    if re.search(r"\bdouble\s+\w+", code):
        problems.append("仍在使用 double 表示金额（PREC-003）")
    if code.count("{") != code.count("}"):
        problems.append("花括号不平衡（{} 个 {{ / {} 个 }}）".format(
            code.count("{"), code.count("}")))
    return problems


class BaseAdapter:
    id = "base"
    label = "基础适配器"
    ai = False

    def status(self) -> dict:
        return {"ready": False, "reason": "未实现"}

    def generate(self, context: dict) -> dict:
        raise NotImplementedError

    def _meta(self, code: str, usage: dict = None, self_check: list = None) -> dict:
        return {
            "code": code,
            "adapter": self.id,
            "label": self.label,
            "ai_generated": self.ai,
            "usage": usage or {},
            "self_check": self_check or [],
        }
