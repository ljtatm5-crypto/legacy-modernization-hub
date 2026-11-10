"""规则驱动适配器：按迁移映射表把存量实现变换为目标实现。

这是当前默认实现。它不是 AI 生成，`ai_generated` 恒为 False，
元数据里会如实写出用了哪几条映射、套了哪个模板。
"""
from __future__ import annotations

from pathlib import Path

from .base import BaseAdapter, digest, validate


class RuleBasedAdapter(BaseAdapter):
    id = "rule-based-template-v1"
    label = "规则驱动变换"
    ai = False

    def __init__(self, ok_template: Path, defect_template: Path, semantic_template: Path = None):
        self.ok_template = Path(ok_template)
        self.defect_template = Path(defect_template)
        self.semantic_template = Path(semantic_template) if semantic_template else None

    def status(self) -> dict:
        ready = self.ok_template.exists() and self.defect_template.exists()
        return {
            "ready": ready,
            "reason": "" if ready else "模板缺失",
            "detail": "零外部依赖，确定性可复现",
        }

    def generate(self, context: dict) -> dict:
        mode = context.get("mode", "ok")
        if mode == "semantic" and self.semantic_template and self.semantic_template.exists():
            source = self.semantic_template
            applied = ["MAP-001"]
            note = "受控语义夹具：金额提前 setScale，人工用例全过、算法用例可发现分歧"
        elif mode == "defect":
            source = self.defect_template
            applied = ["MAP-002"]
            note = "故障注入夹具：故意漏掉 MAP-001 事务映射，用于验证闸门拦截能力"
        else:
            source = self.ok_template
            applied = ["MAP-001", "MAP-002", "MAP-003"]
            note = "按迁移映射表变换：事务边界保留、金额改定点数、DAO 兼容层"
        code = source.read_text(encoding="utf-8")
        meta = self._meta(code, self_check=validate(code))
        meta.update({
            "mode": mode,
            "source_template": source.name,
            "applied_mappings": applied,
            "note": note,
            "prompt_digest": digest(source.name + "|" + mode),
        })
        return meta
