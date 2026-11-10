"""行为等价性闸门。

真编译（Java 8 存量 / JDK 17 目标）→ 契约测试 → 特征用例 →
双跑差分（输出 / 异常 / SQL 轨迹）→ AST 语义巡检（JDK Compiler Tree API）。
所有结果写入 artifacts/equiv/，可被后端与 MCP 工具直接读取。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from hublib import (
    ARTIFACTS, AST_BUILD, BUILD, COMMON_SRC, LEGACY_CLASS, LEGACY_SRC,
    MODERN_CLASS, MODERN_SRC, PROJECT, TESTS_SRC, classpath, ensure_dirs,
    iso, log, run, write_json,
)

GATE_KEYS = ["compile", "unit", "char", "dual", "ast"]


def _javac(release: int, out: Path, sources, cp: str | None = None):
    cmd = ["javac", "--release", str(release), "-encoding", "UTF-8",
           "-Xlint:-options", "-d", str(out)]
    if cp:
        cmd += ["-cp", cp]
    cmd += [str(source) for source in sources]
    return run(cmd, cwd=PROJECT)


def build() -> dict:
    ensure_dirs()
    started = time.time()
    stages = []
    jobs = [
        ("common", 8, BUILD / "common", sorted(COMMON_SRC.glob("*.java")), None),
        ("legacy", 8, BUILD / "legacy", sorted(LEGACY_SRC.glob("*.java")), str(BUILD / "common")),
        ("modern", 17, BUILD / "modern", sorted(MODERN_SRC.glob("*.java")), str(BUILD / "common")),
        ("tests", 17, BUILD / "test", sorted(TESTS_SRC.glob("*.java")), str(BUILD / "common")),
    ]
    for name, release, out, sources, cp in jobs:
        result = _javac(release, out, sources, cp)
        stages.append({
            "stage": name,
            "release": release,
            "sources": len(sources),
            "returncode": result.returncode,
            "stderr": result.stderr.strip()[:2000],
        })
        if result.returncode != 0:
            log("ERR", "编译失败 {} · {}".format(name, result.stderr.strip()[:300]))
    ok = all(stage["returncode"] == 0 for stage in stages)
    report = {
        "ok": ok,
        "stages": stages,
        "duration_ms": int((time.time() - started) * 1000),
        "built_at": iso(),
    }
    write_json(ARTIFACTS / "equiv" / "build.json", report)
    log("OK" if ok else "ERR", "真编译完成 · Java 8 存量 + JDK 17 目标 · {} 个阶段".format(len(stages)))
    return report


def _java(main_class: str, *args):
    cp = classpath(BUILD / "common", BUILD / "legacy", BUILD / "modern", BUILD / "test")
    return run(["java", "-cp", cp, main_class, *args], cwd=PROJECT)


def contract_test(impl: str) -> dict:
    result = _java("com.demo.settle.SettleContractTest", impl)
    lines = [line for line in result.stdout.strip().splitlines() if line.startswith("{")]
    if not lines:
        return {"impl": impl, "passed": 0, "failed": 1, "raw": result.stdout.strip(), "stderr": result.stderr.strip()}
    data = json.loads(lines[-1])
    data["returncode"] = result.returncode
    return data


def harness(impl: str, cases_file=None) -> list:
    args = [impl] if cases_file is None else [impl, str(cases_file)]
    result = _java("com.demo.settle.EquivHarness", *args)
    cases = []
    for line in result.stdout.strip().splitlines():
        if line.startswith("{"):
            try:
                cases.append(json.loads(line))
            except ValueError:
                continue
    return cases


def compare(legacy: list, modern: list) -> tuple:
    diffs, harness_error = [], None
    if not legacy or not modern or len(legacy) != len(modern):
        harness_error = "差分 Harness 输出不完整：legacy={} modern={}".format(
            len(legacy), len(modern))
        diffs.append({"case": "*", "dimension": "harness",
                      "legacy": "{} cases".format(len(legacy)),
                      "modern": "{} cases".format(len(modern))})
        log("ERR", harness_error)
    for left, right in zip(legacy, modern):
        for dimension in ("output", "exception", "sql"):
            if left.get(dimension, "") != right.get(dimension, ""):
                diffs.append({"case": left["case"], "dimension": dimension,
                              "amount": left.get("amount"),
                              "legacy": left.get(dimension, ""),
                              "modern": right.get(dimension, "")})
    return diffs, harness_error


def semantic_scan() -> list:
    """扫描失败必须表现为高风险命中，绝不能静默当成「无问题」。"""
    # baseline / coverage-gain 会绕过 cli 直接调 gate，这里必须自己保证 class 已编译
    from hublib import ensure_ast_classes
    ensure_ast_classes()
    target = MODERN_SRC / "ModernSettleService.java"
    result = run(["java", "-cp", str(AST_BUILD), "SemanticScan", str(target)], cwd=PROJECT)
    failure = None
    if result.returncode != 0:
        failure = "语义巡检进程退出码 {} · {}".format(
            result.returncode, (result.stderr or "").strip()[:200])
    else:
        try:
            return json.loads(result.stdout.strip() or "[]")
        except ValueError:
            failure = "语义巡检输出无法解析 · {}".format(result.stdout.strip()[:200])
    if failure:
        log("ERR", failure)
        return [{"rule": "SCAN-ERR", "risk": "high", "file": "SemanticScan",
                 "line": 0, "evidence": failure}]
    return []


def differential() -> dict:
    """人工用例 + 算法用例双跑。

    分开统计是刻意的：只有能说清「人工 5 例漏了、算法 41 例抓到了」，
    才能证明用例生成不是装饰。
    """
    import testgen
    from hublib import ARTIFACTS as ART

    curated_legacy = harness(LEGACY_CLASS)
    curated_modern = harness(MODERN_CLASS)
    curated_diffs, curated_error = compare(curated_legacy, curated_modern)

    cases_file = ART / "test" / "cases.txt"
    generated = testgen.generate(True)
    testgen.write_cases_txt(generated["cases"], cases_file)
    gen_legacy = harness(LEGACY_CLASS, cases_file)
    gen_modern = harness(MODERN_CLASS, cases_file)
    gen_diffs, gen_error = compare(gen_legacy, gen_modern)

    diffs = curated_diffs + gen_diffs
    report = {
        "cases": len(curated_legacy),
        "cases_total": len(curated_legacy) + len(gen_legacy),
        "diff_count": len(diffs),
        "curated": {"cases": len(curated_legacy), "diff_count": len(curated_diffs)},
        "generated": {
            "cases": len(gen_legacy),
            "diff_count": len(gen_diffs),
            "strategy": generated["strategy"],
            "full_space": generated["full_space"],
            "coverage_t2": generated["coverage_t2"],
            "reduction": generated["reduction"],
            "cases_file": "artifacts/test/cases.txt",
            "diffs": gen_diffs[:40],
        },
        "harness_error": curated_error or gen_error,
        "diffs": diffs,
        "legacy": curated_legacy,
        "modern": curated_modern,
        "compared_at": iso(),
    }
    write_json(ARTIFACTS / "equiv" / "dual-run-diff.json", report)
    write_json(ARTIFACTS / "test" / "case-generation.json", {
        "strategy": generated["strategy"],
        "dimensions": generated["dimensions"],
        "full_space": generated["full_space"],
        "case_count": generated["case_count"],
        "coverage_t2": generated["coverage_t2"],
        "reduction": generated["reduction"],
        "classes": generated["classes"],
        "curated_cases": len(curated_legacy),
        "curated_diff_count": len(curated_diffs),
        "generated_diff_count": len(gen_diffs),
        "generated_at": iso(),
    })
    if gen_diffs and not curated_diffs:
        log("WARN", "算法用例发现 {} 处手工用例漏掉的分歧 · 首个 {}".format(
            len(gen_diffs), gen_diffs[0].get("case")))
    return report


def gate() -> dict:
    """返回闸门结论；不通过时由 CLI 负责出补丁与复检。"""
    ensure_dirs()
    build_report = build()
    if not build_report["ok"]:
        return {
            "verdict": "fail", "gate_results": {key: "fail" for key in GATE_KEYS},
            "reason": "compile_failed", "build": build_report,
        }

    unit_modern = contract_test(MODERN_CLASS)
    unit_legacy = contract_test(LEGACY_CLASS)
    diff = differential()
    semantic = semantic_scan()

    findings = list(semantic)
    if diff["diff_count"] > 0:
        findings.append({
            "rule": "SQL-004", "risk": "high", "file": "ModernSettleService.java",
            "line": 0, "evidence": "双跑差分发现 {} 处不一致（输出 / 异常 / SQL 轨迹）".format(diff["diff_count"]),
        })
    high = [item for item in findings if item.get("risk") == "high"]
    unit_ok = (unit_modern.get("failed", 1) == 0 and unit_legacy.get("failed", 1) == 0
               and unit_modern.get("returncode", 1) == 0
               and unit_legacy.get("returncode", 1) == 0)
    harness_ok = not diff.get("harness_error")

    gate_results = {
        "compile": "pass",
        "unit": "pass" if unit_ok else "fail",
        "char": "pass" if diff.get("cases_total", diff["cases"]) >= 5 and harness_ok else "fail",
        "dual": "pass" if diff["diff_count"] == 0 and harness_ok else "fail",
        "ast": "pass" if not high else "fail",
    }
    verdict = "pass" if all(value == "pass" for value in gate_results.values()) else "fail"

    report = {
        "verdict": verdict,
        "gate_results": gate_results,
        "unit": {"legacy": unit_legacy, "modern": unit_modern},
        "diff": {"cases": diff["cases"], "cases_total": diff.get("cases_total"),
                 "diff_count": diff["diff_count"],
                 "curated": diff.get("curated"), "generated": diff.get("generated"),
                 "harness_error": diff.get("harness_error"), "diffs": diff["diffs"]},
        "findings": findings,
        "reason": "all_checks_passed" if verdict == "pass" else "silent_break_detected",
        "checked_at": iso(),
    }
    write_json(ARTIFACTS / "equiv" / "semantic-findings.json", findings)
    write_json(ARTIFACTS / "equiv" / "gate-report.json", report)
    if verdict == "fail":
        # 复检会覆盖主报告，首次失败快照必须单独留档供复盘引用
        write_json(ARTIFACTS / "equiv" / "gate-first-attempt.json", report)
        write_json(ARTIFACTS / "equiv" / "dual-run-diff-first-attempt.json", diff)
    log("OK" if verdict == "pass" else "ERR",
        "闸门结论 {} · 差分 {} 处差异 · 高风险命中 {} 条".format(
            verdict.upper(), diff["diff_count"], len(high)))
    return report
