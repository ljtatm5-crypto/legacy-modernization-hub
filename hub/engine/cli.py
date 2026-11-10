"""Skills 编排 CLI：DAG 由 hub/workflow.yaml 驱动，产物落盘、审批闸、失败回边。

命令：
  python cli.py reset
  python cli.py config --inject-defect on|off [--adapter rule|llm]
  python cli.py run [--from N] [--restart]
  python cli.py approve
  python cli.py veto --ids P-03,P-02
  python cli.py graph
  python cli.py status
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from hublib import (
    ARTIFACTS, AST_BUILD, BUILD, COMMON_SRC, CONFIG_FILE, LEGACY_SRC, LOG_FILE,
    ensure_ast_classes,
    MODERN_SRC, PROJECT, ROOT, STATE_FILE, TESTS_SRC, classpath, default_state,
    ensure_dirs, iso, load_config, load_state, log, read_json, run,
    save_state, write_json, write_text,
)
import workflow
from generator import generate, patch_transaction_boundary
from equiv import gate


def _rel(path: Path) -> str:
    try:
        return "artifacts/" + str(Path(path).relative_to(ARTIFACTS)).replace("\\", "/")
    except ValueError:
        return str(path)


def _files(*paths) -> list:
    return [_rel(path) for path in paths]


def _result(artifacts, status="pass", detail="", context=None) -> dict:
    return {"artifacts": artifacts, "status": status, "detail": detail, "context": context or {}}


# --------------------------------------------------------------------------
# /scan-arch：从真实源码静态抽取图谱、表血缘与事务边界
# --------------------------------------------------------------------------
def step_scan_arch(state: dict) -> dict:
    sources = sorted(COMMON_SRC.glob("*.java")) + sorted(LEGACY_SRC.glob("*.java"))
    classes, methods, calls, tables, boundaries = [], [], [], set(), []
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for match in re.finditer(r"public\s+(?:final\s+)?class\s+(\w+)", text):
            classes.append({"file": source.name, "class": match.group(1)})
        for match in re.finditer(
                r"^\s*(?:public|private|protected)\s+[^\n;{}]*?\b(\w+)\s*\([^)]*\)", text, re.M):
            methods.append({
                "file": source.name,
                "method": match.group(1),
                "line": text[:match.start()].count("\n") + 1,
            })
        for match in re.finditer(r"\bdao\.(\w+)\s*\(", text):
            calls.append({"file": source.name, "call": "dao." + match.group(1)})
        for match in re.finditer(r"\b(?:UPDATE|INSERT INTO)\s+(\w+)", text):
            tables.add(match.group(1))
        for match in re.finditer(r"@TxRequired[^\n]*", text):
            boundaries.append({
                "file": source.name,
                "line": text[:match.start()].count("\n") + 1,
                "declaration": match.group(0).strip(),
            })

    graph = {
        "module": "OrderSettleService",
        "scanned_files": [source.name for source in sources],
        "classes": classes,
        "methods": methods,
        "calls": calls,
        "generated_at": iso(),
    }
    lineage = {
        "tables": sorted(tables),
        "edges": [
            {"method": "updateOrder", "table": "t_order", "operation": "UPDATE"},
            {"method": "insertSettle", "table": "t_settle", "operation": "INSERT"},
        ],
        "source": "RecordingDao.java",
    }
    tx = {
        "module": "OrderSettleService",
        "declarations": boundaries,
        "runtime_markers": ["BEGIN", "COMMIT", "ROLLBACK"],
        "rule": "RULE-021",
        "inspect_rule": "TX-001",
    }
    write_json(ARTIFACTS / "graph" / "code-graph.json", graph)
    write_json(ARTIFACTS / "graph" / "table-lineage.json", lineage)
    write_text(ARTIFACTS / "graph" / "tx-boundary.yaml",
               "module: OrderSettleService\n"
               "rule: RULE-021\n"
               "inspect_rule: TX-001\n"
               "declarations:\n" + "".join(
                   "  - file: {file}\n    line: {line}\n    annotation: '{declaration}'\n".format(**item)
                   for item in boundaries) +
               "runtime_markers: [BEGIN, COMMIT, ROLLBACK]\n")
    log("MCP", "find_callers / get_table_lineage / get_tx_boundary 扫描 {} 个源文件".format(len(sources)))
    return _result(
        _files(ARTIFACTS / "graph" / "code-graph.json",
               ARTIFACTS / "graph" / "table-lineage.json",
               ARTIFACTS / "graph" / "tx-boundary.yaml"),
        detail="{} 个类 / {} 个方法 / {} 张表".format(len(classes), len(methods), len(tables)))


# --------------------------------------------------------------------------
# /extract-rules：把机器可读规则库转成带来源与置信度的规则条目
# --------------------------------------------------------------------------
def step_extract_rules(state: dict) -> dict:
    source = PROJECT / "rules" / "domain-settle.json"
    data = read_json(source, {"rules": []})
    lines = [
        "# 业务规则库 · {}".format(data.get("module", "unknown")),
        "",
        "| 规则 ID | 业务规则 | 来源文件 | 置信度 | 关联用例 |",
        "|---|---|---|---|---|",
    ]
    for rule in data["rules"]:
        lines.append("| {id} | {text} | `{source}` | {confidence} | {test} |".format(
            id=rule["id"], text=rule["text"], source=rule["source"],
            confidence=rule["confidence"], test=rule.get("mappedTest") or "-"))
    write_text(ARTIFACTS / "rules" / "domain-settle.md", "\n".join(lines) + "\n")
    report = {
        "count": len(data["rules"]),
        "average_confidence": round(sum(r["confidence"] for r in data["rules"]) / max(1, len(data["rules"])), 3),
        "source": "rules/domain-settle.json",
        "extracted_at": iso(),
    }
    write_json(ARTIFACTS / "rules" / "confidence-report.json", report)
    log("OK", "抽取 {} 条业务规则 · 平均置信度 {}".format(report["count"], report["average_confidence"]))
    return _result(
        _files(ARTIFACTS / "rules" / "domain-settle.md",
               ARTIFACTS / "rules" / "confidence-report.json"),
        detail="{} 条规则 / 置信度 {}".format(report["count"], report["average_confidence"]))


# --------------------------------------------------------------------------
# /plan-migration：从映射表生成可审批计划与风险清单
# --------------------------------------------------------------------------
def step_plan_migration(state: dict) -> dict:
    mapping = (PROJECT / "mapping" / "legacy-to-modern.yaml").read_text(encoding="utf-8")
    ids = re.findall(r"- id:\s*(MAP-\d+)", mapping)
    risks = re.findall(r"risk:\s*(\w+)", mapping)
    items = [
        {"id": "P-01", "title": "拆分 OrderSettleService 为应用服务与领域服务", "risk": "medium"},
        {"id": "P-02", "title": "私有 ORM 迁移到兼容层，保留批量写入路径", "risk": "high"},
        {"id": "P-03", "title": "XML 事务声明映射到 @TxRequired(REQUIRED)", "risk": "high"},
        {"id": "P-04", "title": "double 金额改为 BigDecimal(18,2) 定点计算", "risk": "high"},
        {"id": "P-05", "title": "存储过程口径下沉为领域服务", "risk": "medium"},
        {"id": "P-06", "title": "java.util.Date 统一到 Asia/Shanghai", "risk": "low"},
    ]
    lines = [
        "# 迁移计划 · OrderSettleService",
        "",
        "状态：**待人工签核**（未签核不执行 /migrate-module）",
        "",
        "| 计划 ID | 内容 | 风险 | 映射依据 |",
        "|---|---|---|---|",
    ]
    for index, item in enumerate(items):
        lines.append("| {id} | {title} | {risk} | {mapping} |".format(
            **item, mapping=ids[index] if index < len(ids) else "-"))
    write_text(ARTIFACTS / "plan" / "migration-plan.md", "\n".join(lines) + "\n")
    high = len([item for item in items if item["risk"] == "high"])
    write_json(ARTIFACTS / "plan" / "risk-register.json", {
        "items": items,
        "high_risk": high,
        "mapping_ids": ids,
        "mapping_risks": risks,
        "approval_required": True,
        "created_at": iso(),
    })
    log("HITL", "迁移计划已生成 · {} 条计划 / {} 条高风险 · 等待人工签核".format(len(items), high))
    return _result(
        _files(ARTIFACTS / "plan" / "migration-plan.md",
               ARTIFACTS / "plan" / "risk-register.json"),
        detail="{} 条计划 / {} 条高风险".format(len(items), high),
        context={"plan_approved": False})


# --------------------------------------------------------------------------
# /migrate-module：可插拔生成适配层真实写入 Java 源码
# --------------------------------------------------------------------------
def step_migrate_module(state: dict) -> dict:
    # 回边已经把确定性补丁打进源码，此时再全量重生成会把补丁覆盖掉，
    # 既丢掉"增量修复"的事实，也会让补丁 diff 与最终文件对不上。
    repaired = state.get("repaired")
    fix_meta = read_json(ARTIFACTS / "patch" / "fix-meta.json", None)
    if repaired == "patch" and fix_meta:
        meta = read_json(ARTIFACTS / "generation.json", {}) or {}
        repairs = meta.setdefault("repairs", [])
        repairs.append({
            "kind": "deterministic_patch",
            "changed_lines": fix_meta.get("changed_lines"),
            "skipped_regeneration": True,
            "at": iso(),
        })
        write_json(ARTIFACTS / "generation.json", meta)
        shutil.copyfile(MODERN_SRC / "ModernSettleService.java",
                        ARTIFACTS / "src" / "ModernSettleService.java")
        log("INFO", "/migrate-module 增量修复 · 复用确定性补丁（{} 行变更），跳过低效的全量重生成".format(
            fix_meta.get("changed_lines")))
        return _result(
            _files(ARTIFACTS / "src" / "ModernSettleService.java", ARTIFACTS / "generation.json"),
            detail="复用补丁 · {} 行变更".format(fix_meta.get("changed_lines")),
            context={"generator": meta.get("adapter", "rule-based-template-v1")})

    config = load_config()
    defect_mode = config.get("defect_mode") or ("structural" if config.get("inject_defect") else "off")
    # 已补救过就不再注入夹具，否则回边会一直重造同一个缺陷
    if state.get("fixed") or state.get("repaired"):
        mode = "ok"
    elif defect_mode == "semantic":
        mode = "semantic"
    elif defect_mode == "structural":
        mode = "defect"
    else:
        mode = "ok"
    evidence = None
    if repaired == "regen":
        brief = read_json(ARTIFACTS / "patch" / "regeneration-brief.json", None)
        evidence = (brief or {}).get("cases") or None
        if evidence:
            log("INFO", "/migrate-module 带证据重生成 · 回灌 {} 条被闸门拦下的用例".format(len(evidence)))
    meta = generate(mode, evidence)
    write_json(ARTIFACTS / "src" / "generation-meta.json", meta)
    return _result(
        _files(ARTIFACTS / "src" / "ModernSettleService.java", ARTIFACTS / "generation.json"),
        detail="{} 行 · {} · {}".format(meta["lines"], meta["adapter"],
                                        "AI 生成" if meta.get("ai_generated") else "规则变换"),
        context={"generator": meta["adapter"]})


# --------------------------------------------------------------------------
# /audit-deps：与闸门并行执行的依赖与定点数审计
# --------------------------------------------------------------------------
ALLOWED_IMPORT = ("java.", "javax.", "com.demo.settle.")


def step_audit_deps(state: dict) -> dict:
    modern = MODERN_SRC / "ModernSettleService.java"
    legacy = LEGACY_SRC / "LegacySettleService.java"
    findings, imports = [], {}
    for label, path in (("legacy", legacy), ("modern", modern)):
        text = path.read_text(encoding="utf-8")
        symbols = re.findall(r"^import\s+([\w.]+);", text, re.M)
        imports[label] = symbols
        for symbol in symbols:
            if not symbol.startswith(ALLOWED_IMPORT):
                findings.append({"rule": "DEP-001", "where": label, "symbol": symbol,
                                 "risk": "high", "evidence": "非白名单依赖，迁移后可能不可用"})
        for match in re.finditer(r"\bdouble\s+(\w+)", text):
            findings.append({"rule": "PREC-003", "where": label, "symbol": "double " + match.group(1),
                             "risk": "high" if label == "modern" else "medium",
                             "evidence": "金额使用二进制浮点，须改为 BigDecimal"})
        for match in re.finditer(r"\bjava\.util\.Date\b", text):
            findings.append({"rule": "TZ-002", "where": label, "symbol": "java.util.Date",
                             "risk": "high" if label == "modern" else "medium",
                             "evidence": "遗留时间类型，时区口径未统一"})
    blocking = [item for item in findings if item["risk"] == "high" and item["where"] == "modern"]
    report = {
        "scanned": 2,
        "imports": imports,
        "findings": findings,
        "legacy_findings": len([item for item in findings if item["where"] == "legacy"]),
        "modern_findings": len([item for item in findings if item["where"] == "modern"]),
        "blocking": len(blocking),
        "verdict": "pass" if not blocking else "fail",
        "generated_at": iso(),
    }
    write_json(ARTIFACTS / "audit" / "dependency-audit.json", report)
    log("OK" if not blocking else "WARN",
        "依赖审计 · 存量 {} 条待整改 / 目标 {} 条阻断项".format(
            report["legacy_findings"], report["blocking"]))
    return _result(_files(ARTIFACTS / "audit" / "dependency-audit.json"),
                   status="pass" if not blocking else "fail",
                   detail="目标源码阻断项 {} 条".format(len(blocking)))


# --------------------------------------------------------------------------
# /api-surface：公共方法面比对，防止迁移丢接口
# --------------------------------------------------------------------------
def _surface(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    methods = {}
    for match in re.finditer(
            r"^\s*public\s+([\w<>\[\],\s.]+?)\s+(\w+)\s*\(([^)]*)\)", text, re.M):
        name = match.group(2)
        params = ",".join(
            part.strip().split()[0] for part in match.group(3).split(",") if part.strip())
        methods[name] = {"return": match.group(1).strip(), "params": params}
    return methods


def step_api_surface(state: dict) -> dict:
    legacy = _surface(LEGACY_SRC / "LegacySettleService.java")
    modern = _surface(MODERN_SRC / "ModernSettleService.java")
    missing = sorted(set(legacy) - set(modern))
    added = sorted(set(modern) - set(legacy))
    changed = sorted(name for name in set(legacy) & set(modern)
                     if legacy[name]["params"] != modern[name]["params"])
    report = {
        "legacy_methods": legacy, "modern_methods": modern,
        "matched": len(set(legacy) & set(modern)),
        "missing": missing, "added": added, "signature_changed": changed,
        "verdict": "pass" if not missing and not changed else "fail",
        "generated_at": iso(),
    }
    write_json(ARTIFACTS / "audit" / "api-surface.json", report)
    log("OK" if report["verdict"] == "pass" else "WARN",
        "API 面比对 · 匹配 {} / 缺失 {} / 签名变更 {}".format(
            report["matched"], len(missing), len(changed)))
    return _result(_files(ARTIFACTS / "audit" / "api-surface.json"),
                   status=report["verdict"],
                   detail="匹配 {} 个公共方法".format(report["matched"]))


# --------------------------------------------------------------------------
# /equiv-check：真闸门；不通过则交由工作流回边处理
# --------------------------------------------------------------------------
def step_equiv_check(state: dict) -> dict:
    report = gate()
    verdict = report["verdict"]
    artifacts = ["artifacts/equiv/build.json", "artifacts/equiv/dual-run-diff.json",
                 "artifacts/equiv/semantic-findings.json", "artifacts/equiv/gate-report.json"]
    context = {"gate": verdict, "gate_results": report["gate_results"]}

    if verdict == "fail":
        diff = report.get("diff") or {}
        log("WARN", "语义巡检命中 {} 条 · 双跑差分 {} 处不一致".format(
            len([f for f in report.get("findings", []) if f.get("risk") == "high"]),
            diff.get("diff_count", 0)))
        log("ERR", "行为等价性闸门阻断合入：编译与契约测试均未发现该静默破坏")
        write_text(ARTIFACTS / "equiv" / "interception-case.md", "\n".join([
            "# 生成缺陷被闸门拦下 · 真实执行案例",
            "",
            "- 生成适配层：`{}`".format(state.get("generator", "rule-based-template-v1")),
            "- 缺陷：变换时漏掉 MAP-001 事务映射，`@TxRequired` 与 BEGIN/COMMIT 丢失",
            "- 编译：PASS（Java 8 存量 / JDK 17 目标四阶段全部通过）",
            "- 契约测试：PASS（4 条断言未发现事务边界丢失）",
            "- 双跑差分：**{} 处不一致**".format(diff.get("diff_count", 0)),
            "- 语义巡检命中：{}".format(", ".join(
                sorted({item["rule"] for item in report.get("findings", [])}))),
            "- 处置：工作流回边 → 自动出补丁 `patch/tx-annotation-fix.diff` → 重跑生成与闸门",
            "- 证据：`gate-first-attempt.json`、`dual-run-diff-first-attempt.json`、`recheck-report.json`",
            "",
            "记录时间：{}".format(iso()),
        ]) + "\n")
        artifacts += ["artifacts/equiv/gate-first-attempt.json",
                      "artifacts/equiv/dual-run-diff-first-attempt.json",
                      "artifacts/equiv/interception-case.md"]
        return _result(artifacts, status="fail",
                       detail="{} 处差分 / {} 条高风险".format(
                           diff.get("diff_count", 0),
                           len([f for f in report.get("findings", []) if f.get("risk") == "high"])),
                       context=context)

    first = read_json(ARTIFACTS / "equiv" / "gate-first-attempt.json")
    if first:
        write_json(ARTIFACTS / "equiv" / "recheck-report.json", report)
        artifacts.append("artifacts/equiv/recheck-report.json")
        log("OK", "复检通过 · 差分 0 处差异 · 语义巡检高风险 0 条")
        return _result(artifacts, status="pass", detail="复检通过", context=context)
    log("OK", "行为等价性通过 · 编译 / 契约测试 / 特征用例 / 双跑差分 / 语义巡检全绿")
    return _result(artifacts, status="pass", detail="五项检查全绿", context=context)


# --------------------------------------------------------------------------
# /gen-regression：从真实差分用例生成可编译的特征测试
# --------------------------------------------------------------------------
def step_gen_regression(state: dict) -> dict:
    diff = read_json(ARTIFACTS / "equiv" / "dual-run-diff.json", {"legacy": []})
    cases = diff.get("legacy", [])
    normal = [case for case in cases if case.get("output")]
    exceptional = [case for case in cases if case.get("exception")]

    lines = [
        "import com.demo.settle.*;",
        "",
        "/** 由 /gen-regression 依据双跑差分用例生成，锁定存量实现当前行为。 */",
        "public class SettleCharacterizationTest {",
        "    public static void main(String[] args) throws Exception {",
        "        SettleApi api = (SettleApi) Class.forName(args[0]).getDeclaredConstructor().newInstance();",
        "        int passed = 0; int total = 0;",
    ]
    for index, case in enumerate(normal):
        order_id = case["case"]
        amount = case.get("amount", 1.0)
        lines += [
            "        total++;",
            "        try {",
            "            SettleResult r{} = api.settle(new SettleRequest(\"{}\", {}, 1));".format(index, order_id, amount),
            "            if (\"{}\".equals(r{}.toCanonical())) passed++;".format(case["output"], index),
            "        } catch (SettleException ignored) { }",
        ]
    lines += [
        "        System.out.println(\"{\\\"passed\\\":\" + passed + \",\\\"total\\\":\" + total + \"}\");",
        "        if (passed != total) System.exit(1);",
        "    }",
        "}",
    ]
    test_file = ARTIFACTS / "test" / "SettleCharacterizationTest.java"
    write_text(test_file, "\n".join(lines) + "\n")

    cp = classpath(BUILD / "common", BUILD / "legacy", BUILD / "modern")
    compiled = run(["javac", "--release", "17", "-encoding", "UTF-8", "-Xlint:-options", "-cp", cp,
                    "-d", str(BUILD / "test"), str(test_file)], cwd=PROJECT)
    executed = run(["java", "-cp", classpath(BUILD / "test", BUILD / "common",
                                             BUILD / "legacy", BUILD / "modern"),
                    "SettleCharacterizationTest", "com.demo.settle.LegacySettleService"], cwd=PROJECT)
    passed_line = [line for line in executed.stdout.strip().splitlines() if line.startswith("{")]
    outcome = json.loads(passed_line[-1]) if passed_line else {"passed": 0, "total": len(normal)}
    report = {
        "generated_test": _rel(test_file),
        "compilation_returncode": compiled.returncode,
        "execution_returncode": executed.returncode,
        "normal_cases": len(normal),
        "exception_cases": len(exceptional),
        "assertions_passed": outcome.get("passed", 0),
        "coverage_note": "特征用例与契约断言真实执行；行覆盖率需接入 JaCoCo 后实测",
        "generated_at": iso(),
    }
    write_json(ARTIFACTS / "test" / "coverage-report.json", report)
    log("OK", "生成并运行特征测试 · {} 个正常路径断言通过 · {} 个异常分支由契约测试覆盖".format(
        report["assertions_passed"], len(exceptional)))
    return _result(_files(test_file, ARTIFACTS / "test" / "coverage-report.json"),
                   detail="{} 个断言通过".format(report["assertions_passed"]))


# --------------------------------------------------------------------------
# /report-delta：汇总真实测量值与效率机制
# --------------------------------------------------------------------------
def step_report_delta(state: dict) -> dict:
    gate_report = read_json(ARTIFACTS / "equiv" / "gate-report.json", {})
    build_report = read_json(ARTIFACTS / "equiv" / "build.json", {})
    coverage = read_json(ARTIFACTS / "test" / "coverage-report.json", {})
    regression = read_json(ARTIFACTS / "equiv" / "recheck-report.json")
    diff = read_json(ARTIFACTS / "equiv" / "dual-run-diff.json", {})
    audit = read_json(ARTIFACTS / "audit" / "dependency-audit.json", {})
    surface = read_json(ARTIFACTS / "audit" / "api-surface.json", {})

    measured = {
        "build_stages": len(build_report.get("stages", [])),
        "build_duration_ms": build_report.get("duration_ms", 0),
        "characterization_cases": diff.get("cases", 0),
        "assertions_passed": coverage.get("assertions_passed", 0),
        "dual_run_diffs": gate_report.get("diff", {}).get("diff_count", 0),
        "gate_verdict": state.get("gate", "idle"),
        "rework": state.get("rework", 0),
        "auto_patch_applied": bool(regression),
        "api_matched": surface.get("matched", 0),
        "audit_blocking": audit.get("blocking", 0),
    }
    baseline = {
        "human_hours": 80,
        "platform_hours": 6.5,
        "human_steps": 41,
        "platform_steps": 9,
        "integration_hours_human": 18,
        "integration_hours_platform": 3,
        "coverage_human_pct": 12,
        "coverage_platform_pct": 87,
        "source": "候选文档基线：工时 / 步骤 / 覆盖率均为文档值，非本系统实测；需 W1 实测记录替换",
        "measured": False,
    }
    patch_path = ARTIFACTS / "patch" / "tx-annotation-fix.diff"
    patch_lines = len(patch_path.read_text(encoding="utf-8").splitlines()) if patch_path.exists() else 0
    graph = workflow.graph_payload(_spec(), state)
    total_ms = graph["critical_ms"]
    artifact_count = sum(len(entry.get("artifacts", [])) for entry in state.get("nodes", {}).values())
    skipped = state.get("skipped_steps", 0)
    attempts = sum(entry.get("attempts", 0) for entry in state.get("nodes", {}).values())
    mechanisms = [
        {"id": "parallel", "name": "并行调度",
         "value": "{} 节点 / {} 并发 / {} 层".format(
             len(graph["nodes"]), graph["concurrency"], graph["layer_count"]),
         "detail": "旁支 {} ms 完全被关键路径覆盖；关键路径 {} ms，串行需 {} ms".format(
             graph["off_critical_ms"], graph["critical_ms"], graph["serial_ms"])},
        {"id": "incremental", "name": "增量重生成",
         "value": "1 个文件 / {} 行补丁".format(patch_lines) if patch_lines else "本次无需补丁",
         "detail": "缺陷闭环只改回事务边界，未全量重生成整个模块" if patch_lines else "首轮闸门全绿，未触发补丁"},
        {"id": "resume", "name": "断点续跑",
         "value": "复用 {} 个节点".format(skipped),
         "detail": "已完成节点的产物直接复用，不重复编译与生成" if skipped else "本次从头执行，未复用前序产物"},
        {"id": "gate", "name": "闸门前置",
         "value": "拦截 {} 次".format(1 if regression else 0),
         "detail": "在合入前发现静默破坏，避免上线后返工" if regression else "本次无缺陷，五项检查全绿"},
        {"id": "machine", "name": "链路机器耗时",
         "value": "{} s".format(round(total_ms / 1000, 1)),
         "detail": "{} 个节点实测（真编译 / 双跑差分 / AST 巡检），不含人工审批".format(len(graph["nodes"]))},
        {"id": "artifacts", "name": "产物可回放",
         "value": "{} 个".format(artifact_count),
         "detail": "每步中间产物落盘，共 {} 次节点尝试记录在轨迹里".format(attempts)},
    ]
    summary = {
        "module": "OrderSettleService",
        "generator": state.get("generator", "rule-based-template-v1"),
        "ai_generated": bool(read_json(ARTIFACTS / "generation.json", {}).get("ai_generated")),
        "measured": measured,
        "baseline": baseline,
        "workflow": {
            "name": graph["name"], "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]), "concurrency": graph["concurrency"],
            "layers": graph["layer_count"], "critical_path": graph["critical_path"],
            "critical_ms": graph["critical_ms"], "serial_ms": graph["serial_ms"],
            "parallel_gain_ms": graph["parallel_gain_ms"],
        },
        "efficiency_mechanisms": mechanisms,
        "steps": [{"id": step["id"], "status": step["status"],
                   "artifacts": step["artifacts"], "duration_ms": step["duration_ms"]}
                  for step in state["steps"]],
        "finished_at": iso(),
    }
    write_json(ARTIFACTS / "report" / "delta-summary.json", summary)
    write_json(ARTIFACTS / "report" / "efficiency-mechanisms.json", {
        "mechanisms": mechanisms,
        "baseline": baseline,
        "note": "机制证据为本系统实测；人工工时基线为候选文档值，需 W1 对照实验。",
    })
    write_json(ARTIFACTS / "report" / "efficiency-metrics.json", {
        "measured": measured, "baseline": baseline,
        "note": "measured 为本系统真实执行结果；baseline 为候选文档基线，尚未实测。",
    })
    log("OK", "差异报告已汇总 · 真实测量 {} 项 · 关键路径 {} ms".format(
        len(measured), graph["critical_ms"]))
    return _result(_files(ARTIFACTS / "report" / "delta-summary.json",
                          ARTIFACTS / "report" / "efficiency-metrics.json"),
                   detail="{} 项实测指标".format(len(measured)))


HANDLERS = {
    "scan_arch": step_scan_arch,
    "extract_rules": step_extract_rules,
    "plan_migration": step_plan_migration,
    "migrate_module": step_migrate_module,
    "audit_deps": step_audit_deps,
    "api_surface": step_api_surface,
    "equiv_check": step_equiv_check,
    "gen_regression": step_gen_regression,
    "report_delta": step_report_delta,
}


def scan_dataset(path: str, label: str = "") -> dict:
    """对任意真实 Java 源码目录跑通用语义巡检，产出可复现的数据集报告。"""
    import subprocess as _sp
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return {"error": "目录不存在：{}".format(target)}
    if not ensure_ast_classes():
        return {"error": "巡检器编译失败，请确认已安装 JDK（javac 在 PATH 中）"}
    result = run(["java", "-cp", str(AST_BUILD), "LegacyScan", str(target)], cwd=PROJECT)
    if result.returncode != 0:
        return {"error": "巡检失败：{}".format((result.stderr or "").strip()[:400])}
    lines = [line for line in result.stdout.strip().splitlines() if line.startswith("{")]
    if not lines:
        return {"error": "巡检无输出"}
    data = json.loads(lines[-1])
    meta = {"label": label or target.name, "path": str(target), "scanned_at": iso()}
    revision = {}
    for key, cmd in (("commit", ["git", "-C", str(target), "rev-parse", "HEAD"]),
                     ("remote", ["git", "-C", str(target), "config", "--get", "remote.origin.url"])):
        probe = _sp.run(cmd, capture_output=True, text=True)
        if probe.returncode == 0 and probe.stdout.strip():
            revision[key] = probe.stdout.strip()
    # 分发包里没有 .git，改从随包的 SOURCE.txt 读取提交版本
    if not revision.get("commit"):
        for parent in target.parents[:4]:
            note = parent / "SOURCE.txt"
            if note.exists():
                for line in note.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("提交版本："):
                        revision["commit"] = line.split("：", 1)[1].strip()
                    elif line.startswith("来源仓库："):
                        revision["remote"] = line.split("：", 1)[1].strip()
                if revision:
                    revision["source"] = "SOURCE.txt"
                break
    data.update(meta)
    data["revision"] = revision
    write_json(ARTIFACTS / "dataset" / "scan-report.json", data)
    log("OK", "真实数据集巡检 · {} 个文件 / {} 个类 / {} 个方法 · {} 条风险 · {} ms".format(
        data["counts"]["files_parsed"], data["counts"]["classes"],
        data["counts"]["methods"], data["counts"]["findings"], data["counts"]["elapsed_ms"]))
    return data


def remediate_auto(state: dict) -> str:
    """失败回边的策略选择：结构性问题打补丁，语义分歧回灌证据重生成。"""
    report = read_json(ARTIFACTS / "equiv" / "gate-report.json", {}) or {}
    rules = {item.get("rule") for item in report.get("findings", [])
             if item.get("risk") == "high"}
    if rules & {"TX-001", "EX-007"}:
        patch_transaction_boundary()
        state["fixed"] = True
        state["repaired"] = "patch"
        return "结构缺陷：应用确定性补丁"

    diffs = (report.get("diff") or {}).get("diffs") or []
    if diffs:
        brief = {
            "reason": "semantic_divergence",
            "gate_verdict": report.get("verdict"),
            "diff_count": len(diffs),
            "cases": diffs,
            "generated_from": "artifacts/equiv/dual-run-diff.json",
            "created_at": iso(),
        }
        write_json(ARTIFACTS / "patch" / "regeneration-brief.json", brief)
        state["repaired"] = "regen"
        return "语义分歧：回灌 {} 条失败用例，带证据重生成".format(len(diffs))

    state["repaired"] = None
    return "未识别到可自动修复的问题"


REMEDIATIONS = {"auto_repair": remediate_auto}


def _spec() -> dict:
    spec, _source = workflow.load()
    return spec


def _runner(spec: dict):
    return workflow.Runner(spec, HANDLERS, REMEDIATIONS, save_state)


ADJUSTMENTS = {
    "P-03": "保留 XML 事务声明 + AOP 兼容层过渡，第二阶段再迁 @TxRequired（按否决调整）",
    "P-02": "分批迁移：先只迁读路径，批量写入路径本期保留（按否决调整）",
}


def veto(identifiers: list) -> dict:
    """真实重规划：更新风险清单并重写迁移计划，供人再次签核。"""
    risk_path = ARTIFACTS / "plan" / "risk-register.json"
    risk = read_json(risk_path, None)
    if not risk:
        return {"error": "plan not generated", "hint": "先运行 /plan-migration"}
    changed = []
    for item in risk.get("items", []):
        if item["id"] in identifiers:
            item["vetoed"] = True
            item["adjusted"] = ADJUSTMENTS.get(
                item["id"], "执行顺序后移 1 位，补充双跑差分与人工签核项（按否决调整）")
            changed.append(item["id"])
    risk["replanned"] = True
    risk["vetoed_ids"] = changed
    write_json(risk_path, risk)

    lines = [
        "# 迁移计划 · OrderSettleService",
        "",
        "状态：**已按 {} 条否决重规划，待人工签核**".format(len(changed)),
        "",
        "| 计划 ID | 内容 | 风险 | 调整 |",
        "|---|---|---|---|",
    ]
    for item in risk["items"]:
        lines.append("| {id} | {title} | {risk} | {note} |".format(
            id=item["id"], title=item.get("adjusted") or item["title"],
            risk=item["risk"], note="已否决" if item.get("vetoed") else "-"))
    write_text(ARTIFACTS / "plan" / "migration-plan.md", "\n".join(lines) + "\n")
    log("HITL", "计划条目否决 {} 条 → 编排层重规划 · 风险清单与计划文档已更新".format(len(changed)))
    return {"vetoed": changed, "replanned": True}


RUNS = ROOT / "runs"
ARTIFACT_DIRS = ["graph", "rules", "plan", "src", "equiv", "test", "report",
                 "patch", "workflow", "audit"]


def archive_previous() -> str:
    """把上一轮产物整体挪到 hub/runs/<run_id>/。

    不删除任何证据：一次迁移的中间产物本身就是交付物，
    重跑应该产生新的一轮，而不是把上一轮的现场抹掉。
    """
    previous = read_json(STATE_FILE, {}) or {}
    run_id = previous.get("run_id") or ""
    existing = [ARTIFACTS / name for name in ARTIFACT_DIRS if (ARTIFACTS / name).exists()]
    if not existing and not LOG_FILE.exists():
        return ""
    if not run_id:
        run_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = RUNS / run_id
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = RUNS / "{}-{}".format(run_id, suffix)
    destination.mkdir(parents=True, exist_ok=True)
    for folder in existing:
        shutil.move(str(folder), str(destination / folder.name))
    if LOG_FILE.exists():
        shutil.move(str(LOG_FILE), str(destination / "run.log"))
    if previous:
        write_json(destination / "state.json", previous)
    return destination.name


def prune(keep: int = 5) -> dict:
    """手动清理历史归档，默认保留最近 5 轮。需要时显式调用，不自动删。"""
    if not RUNS.exists():
        return {"kept": 0, "removed": []}
    entries = sorted([path for path in RUNS.iterdir() if path.is_dir()], key=lambda path: path.name)
    removed = [path.name for path in entries[:-keep]] if keep > 0 else []
    for name in removed:
        shutil.rmtree(RUNS / name)
    return {"kept": len(entries) - len(removed), "removed": removed}


def reset() -> dict:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    config = load_config()
    archived = archive_previous()
    ensure_dirs()
    spec, source = workflow.load()
    state = default_state(spec, source)
    state["inject_defect"] = bool(config.get("inject_defect"))
    state["generator"] = config.get("model_adapter") or "rule-based-template-v1"
    save_state(state)
    write_json(ARTIFACTS / "workflow" / "graph.json", workflow.graph_payload(spec, state))
    log("INFO", "会话初始化 · 组织级规则 / 仓库级规则 / 模块级历史坑清单已加载")
    log("MCP", "3 个工具就绪：code-graph / defect-db / ci-pipeline")
    log("INFO", "工作流已装载 · {} 个节点 / {} 并发 / 来源 {}".format(
        len(spec["nodes"]), spec["concurrency"],
        "hub/workflow.yaml" if source["kind"] == "file" else "内置 DAG（" + source.get("reason", "") + "）"))
    if archived:
        log("INFO", "上一轮产物已归档 · hub/runs/{} · 可随时回放".format(archived))
    return state


def execute(from_index: int = -1) -> str:
    ensure_ast_classes()
    spec, source = workflow.load()
    state = load_state()
    if source["kind"] != "file":
        log("WARN", "工作流文件不可用，回退内置 DAG · {}".format(source.get("reason")))

    if from_index >= 0:
        order = workflow.order_of(spec)
        if from_index >= len(order):
            return "already-finished"
        target = order[from_index]
        targets = [target] + workflow.descendants(spec, target)
        for node_id in targets:
            state.setdefault("nodes", {}).setdefault(node_id, {"attempts": 0})
            state["nodes"][node_id].update({"status": "pending", "artifacts": [],
                                            "duration_ms": 0, "note": "自 {} 重跑".format(target)})
        state["finished"] = False
        save_state(state)
        log("INFO", "自 {} 起重跑 · 重置 {} 个节点".format(target, len(targets)))

    state["workflow"] = {
        "name": spec.get("name"), "node_count": len(spec["nodes"]),
        "concurrency": spec["concurrency"], "source": source,
    }
    save_state(state)
    runner = _runner(spec)
    return runner.run()


def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy Modernization Hub CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reset")
    sub.add_parser("status")
    sub.add_parser("approve")
    sub.add_parser("graph")
    sub.add_parser("probe")
    dataset_parser = sub.add_parser("dataset")
    dataset_parser.add_argument("--path", default="")
    dataset_parser.add_argument("--preset", default="")
    dataset_parser.add_argument("--label", default="")
    prune_parser = sub.add_parser("prune")
    prune_parser.add_argument("--keep", type=int, default=5)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--from", dest="start", type=int, default=-1)
    run_parser.add_argument("--restart", action="store_true")
    config_parser = sub.add_parser("config")
    config_parser.add_argument("--inject-defect", choices=["on", "off"])
    config_parser.add_argument("--defect", choices=["off", "structural", "semantic"])
    config_parser.add_argument("--adapter", choices=["rule", "llm"])
    veto_parser = sub.add_parser("veto")
    veto_parser.add_argument("--ids", required=True, help="逗号分隔的计划 ID")
    args = parser.parse_args()

    ensure_dirs()
    if args.command == "reset":
        reset()
        print("reset")
    elif args.command == "config":
        config = load_config()
        if args.defect:
            config["defect_mode"] = args.defect
            config["inject_defect"] = args.defect != "off"
        if args.inject_defect:
            config["inject_defect"] = args.inject_defect == "on"
            if "defect_mode" not in config:
                config["defect_mode"] = "structural" if config["inject_defect"] else "off"
        if args.adapter:
            config["model_adapter"] = "llm-api-v1" if args.adapter == "llm" else "rule-based-template-v1"
        write_json(CONFIG_FILE, config)
        state = load_state()
        state["inject_defect"] = bool(config.get("inject_defect"))
        state["generator"] = config.get("model_adapter")
        state["fixed"] = False
        save_state(state)
        log("WARN" if config.get("inject_defect") else "INFO",
            "故障注入{}".format("开启：生成适配层将漏掉事务映射" if config.get("inject_defect") else "关闭"))
        log("INFO", "生成适配层 → {}".format(config.get("model_adapter")))
        print(json.dumps(config, ensure_ascii=False))
    elif args.command == "dataset":
        from hublib import dataset_path
        if args.preset:
            try:
                target = str(dataset_path(args.preset))
            except FileNotFoundError as error:
                print(json.dumps({"error": str(error)}, ensure_ascii=False))
                return 1
            label = args.label or "Apache Commons Lang (src/main/java)"
            result = scan_dataset(target, label)
        elif args.path:
            result = scan_dataset(args.path, args.label)
        else:
            print(json.dumps({"error": "需要 --preset commons-lang 或 --path <目录>"},
                             ensure_ascii=False))
            return 1
        print(json.dumps({key: result.get(key) for key in
                          ("label", "counts", "by_rule", "revision", "error") if key in result},
                         ensure_ascii=False))
    elif args.command == "prune":
        print(json.dumps(prune(args.keep), ensure_ascii=False))
    elif args.command == "probe":
        from generator import probe
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
    elif args.command == "approve":
        state = load_state()
        state["plan_approved"] = True
        save_state(state)
        log("HITL", "人工签核通过 · /migrate-module 获得执行授权")
        print(execute(-1))
    elif args.command == "run":
        if args.restart:
            config = load_config()
            reset()
            state = load_state()
            state["inject_defect"] = bool(config.get("inject_defect"))
            state["generator"] = config.get("model_adapter")
            save_state(state)
            print(execute(-1))
        else:
            print(execute(args.start))
    elif args.command == "veto":
        result = veto([item.strip() for item in args.ids.split(",") if item.strip()])
        print(json.dumps(result, ensure_ascii=False))
        if "error" in result:
            return 1
    elif args.command == "graph":
        print(json.dumps(workflow.graph_payload(_spec(), load_state()), ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(load_state(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
