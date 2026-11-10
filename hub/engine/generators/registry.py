"""生成适配器注册表：解析配置、探测可用性、必要时回退并说明原因。"""
from __future__ import annotations

from hublib import ROOT, TEMPLATES, log

from .base import AdapterError
from .llm import LLMAdapter
from .rule_based import RuleBasedAdapter

RULE = RuleBasedAdapter(TEMPLATES / "modern_ok.java.tpl",
                        TEMPLATES / "modern_defect.java.tpl",
                        TEMPLATES / "modern_semantic_defect.java.tpl")
LLM = LLMAdapter(ROOT / "model.json")
ADAPTERS = {RULE.id: RULE, LLM.id: LLM}
DEFAULT_ID = RULE.id
DEFAULT_CONFIG = ROOT / "model.json"


def get(adapter_id: str):
    return ADAPTERS.get(adapter_id or DEFAULT_ID) or ADAPTERS[DEFAULT_ID]


def resolve(config: dict) -> tuple:
    """返回 (adapter, fallback_reason)。fallback_reason 为空表示按配置命中。"""
    wanted = config.get("model_adapter") or DEFAULT_ID
    adapter = ADAPTERS.get(wanted)
    if adapter is None:
        return RULE, "未知适配器 {}，回退规则适配器".format(wanted)
    status = adapter.status()
    if not status.get("ready"):
        return RULE, status.get("reason") or "适配器不可用"
    return adapter, ""


def probe() -> dict:
    items = []
    for adapter in ADAPTERS.values():
        status = adapter.status()
        items.append({
            "id": adapter.id, "label": adapter.label, "ai": adapter.ai,
            "ready": status.get("ready"), "reason": status.get("reason", ""),
            "base_url": status.get("base_url", ""), "model": status.get("model", ""),
            "key_present": status.get("key_present"),
        })
    active, fallback = resolve({"model_adapter": _configured()})
    return {
        "configured": _configured(),
        "active": active.id,
        "fallback_reason": fallback,
        "adapters": items,
    }


def _configured() -> str:
    from hublib import load_config
    return load_config().get("model_adapter") or DEFAULT_ID
