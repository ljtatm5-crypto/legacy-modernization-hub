"""可插拔生成适配层。

当前有两个实现：
- rule-based-template-v1：规则驱动模板变换，零依赖、确定性可复现，默认启用；
- llm-api-v1：调用 OpenAI 兼容接口，密钥从环境变量读取，需显式配置后启用。

两者对上层暴露同一个 generate(mode) 接口，切换只改 artifacts/config.json
里的 model_adapter，不动任何调用方代码。
"""
from __future__ import annotations

import argparse
import difflib
import shutil

from hublib import (
    ARTIFACTS, MODERN_SRC, PROJECT, TEMPLATES, ensure_dirs, iso, load_config,
    log, read_json, write_json, write_text,
)
import codesurface
from generators import registry
from generators.base import AdapterError

ADAPTER_ID = registry.RULE.id  # 默认值，实际以配置为准
OK_TEMPLATE = TEMPLATES / "modern_ok.java.tpl"
TARGET = MODERN_SRC / "ModernSettleService.java"
META = ARTIFACTS / "generation.json"


def _context(mode: str, evidence: list = None) -> dict:
    rules = read_json(PROJECT / "rules" / "domain-settle.json", {}).get("rules", [])
    mapping_path = PROJECT / "mapping" / "legacy-to-modern.yaml"
    legacy_path = PROJECT / "legacy" / "src" / "com" / "demo" / "settle" / "LegacySettleService.java"
    return {
        "mode": mode,
        "rules": rules,
        "api_surface": codesurface.signatures(),
        "mapping": mapping_path.read_text(encoding="utf-8") if mapping_path.exists() else "",
        "legacy_source": legacy_path.read_text(encoding="utf-8") if legacy_path.exists() else "",
        "target_class": "ModernSettleService",
        "evidence": evidence or [],
    }


def generate(mode: str = "ok", evidence: list = None) -> dict:
    ensure_dirs()
    config = load_config()
    adapter, fallback = registry.resolve(config)
    if fallback:
        log("WARN", "生成适配层回退规则实现 · {}".format(fallback))

    if mode in ("defect", "semantic"):
        # 故障注入是测试夹具，与适配器无关，固定走规则模板保证可复现
        adapter = registry.RULE
        fallback = "故障注入固定使用规则夹具" if not fallback else fallback

    try:
        produced = adapter.generate(_context(mode, evidence))
    except AdapterError as error:
        log("ERR", "模型适配器调用失败 · {} · 回退规则实现".format(error))
        adapter, produced = registry.RULE, registry.RULE.generate(_context(mode, evidence))
        fallback = "模型调用失败：{}".format(error)

    content = produced["code"]
    write_text(TARGET, content)
    shutil.copyfile(TARGET, ARTIFACTS / "src" / "ModernSettleService.java")

    meta = {
        "adapter": produced["adapter"],
        "adapter_label": produced.get("label"),
        "requested_adapter": config.get("model_adapter"),
        "fallback_reason": fallback,
        "ai_generated": bool(produced.get("ai_generated")),
        "mode": mode,
        "target": str(TARGET.relative_to(ARTIFACTS.parent)),
        "applied_mappings": produced.get("applied_mappings", []),
        "self_check": produced.get("self_check", []),
        "evidence_count": len(evidence or []),
        "usage": produced.get("usage", {}),
        "latency_ms": produced.get("latency_ms"),
        "attempts": produced.get("attempts", 1),
        "model": produced.get("model"),
        "prompt_digest": produced.get("prompt_digest"),
        "source_template": produced.get("source_template"),
        "note": produced.get("note", ""),
        "generated_at": iso(),
        "lines": len(content.splitlines()),
    }
    write_json(META, meta)
    log("INFO", "/migrate-module 生成 {} 行目标源码 · 适配器 {} · 自检 {} 项问题 · 用量 {}".format(
        meta["lines"], meta["adapter"], len(meta["self_check"]),
        meta["usage"].get("total_tokens", 0) if meta["usage"] else 0))
    if meta["self_check"]:
        log("WARN", "生成自检未通过 · {} → 交由闸门判定".format("；".join(meta["self_check"])))
    return meta


def patch_transaction_boundary() -> dict:
    """真实修复：把缺陷源码替换为正确源码，并生成可审阅的 unified diff。"""
    ensure_dirs()
    before = TARGET.read_text(encoding="utf-8")
    after = OK_TEMPLATE.read_text(encoding="utf-8")
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="a/ModernSettleService.java",
        tofile="b/ModernSettleService.java",
    ))
    write_text(TARGET, after)
    write_text(ARTIFACTS / "patch" / "tx-annotation-fix.diff", diff)
    write_text(ARTIFACTS / "src" / "ModernSettleService.java", after)
    changed = len([line for line in diff.splitlines()
                   if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))])
    write_json(ARTIFACTS / "patch" / "fix-meta.json", {
        "trigger": "TX-001 + SQL-004",
        "action": "restore @TxRequired / dao.begin() / dao.commit() / dao.rollback()",
        "changed_lines": changed,
        "patched_at": iso(),
    })
    log("INFO", "自动补丁 tx-annotation-fix.diff 已写入并应用 · {} 行变更".format(changed))
    return {"diff_file": "artifacts/patch/tx-annotation-fix.diff", "changed_lines": changed}


def probe() -> dict:
    return registry.probe()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["ok", "defect", "patch", "probe"], default="ok")
    args = parser.parse_args()
    if args.mode == "patch":
        print(patch_transaction_boundary())
    elif args.mode == "probe":
        print(probe())
    else:
        print(generate(args.mode))
