"""效率证明：同模块 A/B 对照仪器 + 链路基准测试。

为什么不是一个百分比：
「80 人时 → 6.5 人时」是候选文档里的数字，不是本系统跑出来的。直接把它
印在界面上，一旦被追问"你测过吗"就站不住。所以这里把证明拆成三层：

  第一层（可立即给出）链路机制实测：节点数、并发度、关键路径、补丁行数、
                        拦截次数、产物数、基准批次的耗时分布与一次通过率；
  第二层（需真人）      同模块人工臂 vs 平台臂，同一验收标准，用本仪器打点；
  第三层（不宣称）      文档基线值，只做参照，标 measured=false。

判读规则写在 protocol 里：任一臂未实测就不算缩短比例，只报"待实测"。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from hublib import (
    ARTIFACTS, CONFIG_FILE, ENGINE, LEGACY_CLASS, MODERN_CLASS, MODERN_SRC,
    PROJECT, ROOT, STATE_FILE,
    ensure_dirs, iso, load_config, load_state, log, read_json, write_json, write_text,
)
import workflow
from equiv import build as equiv_build, gate as equiv_gate
from equiv import compare as compare_cases
from equiv import harness as harness_cls

W1 = ARTIFACTS / "w1"
HUMAN_ARM = W1 / "arm-human.json"
PLATFORM_ARM = W1 / "arm-platform.json"
BENCH = W1 / "bench.json"
BASELINE = W1 / "baseline.json"
AB_REPORT = W1 / "ab-report.json"
DOC_BASELINE = {
    "human_hours": 80,
    "platform_hours": 6.5,
    "human_steps": 41,
    "platform_steps": 9,
    "coverage_human_pct": 12,
    "coverage_platform_pct": 87,
    "source": "候选文档基线，未在本系统实测",
}


# --------------------------------------------------------------------------
# 计时工具
# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _human() -> dict:
    return read_json(HUMAN_ARM, {}) or {}


def _record_human(record: dict) -> None:
    write_json(HUMAN_ARM, record)


def human_start(label: str, operator: str) -> dict:
    ensure_dirs()
    record = {
        "arm": "human", "label": label, "operator": operator,
        "started_at": _now(), "ended_at": None,
        "events": [{"at": _now(), "kind": "start", "note": "计时开始"}],
        "paused_ms": 0, "wall_ms": 0, "active_ms": 0,
        "steps": 0, "rework": 0, "first_pass": None,
        "integration_ms": 0, "tokens": 0, "cost": None,
        "measured": True, "state": "running", "notes": "",
    }
    _record_human(record)
    log("HITL", "人工臂计时开始 · {} · 操作者 {}".format(label, operator))
    return record


def human_mark(note: str, steps: int = 0) -> dict:
    record = _human()
    if not record or record.get("state") != "running":
        return {"error": "没有正在计时的人工臂，先执行 human-start"}
    record["events"].append({"at": _now(), "kind": "mark", "note": note,
                             "steps": steps, "elapsed_ms": _elapsed(record)})
    if steps:
        record["steps"] = steps
    _record_human(record)
    return {"marked": note, "elapsed_ms": _elapsed(record), "steps": record["steps"]}


def human_pause(kind: str) -> dict:
    record = _human()
    if not record or record.get("state") not in ("running", "paused"):
        return {"error": "没有可暂停的人工臂"}
    if kind == "pause":
        record["events"].append({"at": _now(), "kind": "pause", "note": "暂停"})
        record["state"] = "paused"
    else:
        record["events"].append({"at": _now(), "kind": "resume", "note": "恢复"})
        record["state"] = "running"
    _record_human(record)
    return {"state": record["state"]}


def _elapsed(record: dict) -> int:
    """有效工时 = 现在 - 开始 - 暂停区间。"""
    if not record.get("started_at"):
        return 0
    total = (time.time() - _parse(record["started_at"])) * 1000
    pauses, opened = 0.0, None
    for event in record.get("events", []):
        if event["kind"] == "pause":
            opened = _parse(event["at"])
        elif event["kind"] == "resume" and opened is not None:
            pauses += (_parse(event["at"]) - opened) * 1000
            opened = None
    if opened is not None:
        pauses += (time.time() - opened) * 1000
    return int(max(0, total - pauses))


def human_stop(rework: int, first_pass: str, integration_min: float, notes: str) -> dict:
    record = _human()
    if not record:
        return {"error": "没有人工臂记录"}
    record["ended_at"] = _now()
    record["active_ms"] = _elapsed(record)
    record["wall_ms"] = record["active_ms"] + int(record.get("paused_ms", 0))
    record["rework"] = rework
    record["first_pass"] = first_pass == "yes"
    record["integration_ms"] = int(integration_min * 60 * 1000)
    record["state"] = "finished"
    record["notes"] = notes
    record["events"].append({"at": _now(), "kind": "stop", "note": "计时结束"})
    _record_human(record)
    log("HITL", "人工臂计时结束 · 有效工时 {} min · 返工 {} 次".format(
        round(record["active_ms"] / 60000, 1), rework))
    return record


def human_import(path: Path) -> dict:
    data = read_json(path, None)
    if not isinstance(data, dict):
        return {"error": "文件不存在或不是 JSON：{}".format(path)}
    data.setdefault("arm", "human")
    data["measured"] = True
    data.setdefault("state", "finished")
    _record_human(data)
    return {"imported": str(path), "active_ms": data.get("active_ms")}


# --------------------------------------------------------------------------
# 基线臂：把存量代码直接贴给模型，拿回结果就用
# --------------------------------------------------------------------------
BASELINE_SYSTEM = "你是一名 Java 工程师。"
MAX_ATTEMPTS = 2


def _baseline_prompt() -> str:
    """刻意只给存量代码 + 业务规则。

    不给接口面、不给硬性约束、不给迁移映射、不给历史坑清单，
    也不做自检与修复重试——这就是「把文件贴进对话框」的真实条件。
    """
    legacy = (PROJECT / "legacy" / "src" / "com" / "demo" / "settle"
              / "LegacySettleService.java").read_text(encoding="utf-8")
    rules = read_json(PROJECT / "rules" / "domain-settle.json", {}).get("rules", [])
    rule_text = "\n".join("- {} {}".format(rule.get("id"), rule.get("text")) for rule in rules)
    return "\n".join([
        "把下面的 Java 8 存量实现迁移到 JDK 17，保持行为完全等价。",
        "输出类名为 ModernSettleService，包名 com.demo.settle。",
        "",
        "[存量代码]",
        legacy,
        "",
        "[业务规则]",
        rule_text or "（无）",
        "",
        "直接输出完整的类源码，不要解释，不要 Markdown 围栏。",
    ])


def _snapshot_equiv() -> dict:
    folder = ARTIFACTS / "equiv"
    folder.mkdir(parents=True, exist_ok=True)
    return {path.name: path.read_bytes() for path in folder.glob("*") if path.is_file()}


def _restore_equiv(snapshot: dict) -> None:
    """基线评测会把 artifacts/equiv 写脏，评测完原样还原，避免污染正式证据。"""
    folder = ARTIFACTS / "equiv"
    for path in folder.glob("*"):
        if path.is_file() and path.name not in snapshot:
            path.unlink()
    for name, data in snapshot.items():
        (folder / name).write_bytes(data)


def baseline(runs: int = 3) -> dict:
    from generators.base import AdapterError
    from generators.llm import LLMAdapter

    adapter = LLMAdapter(ROOT / "model.json")
    status = adapter.status()
    if not status["ready"]:
        return {"error": "模型适配器不可用：{}".format(status["reason"])}

    target = MODERN_SRC / "ModernSettleService.java"
    original = target.read_bytes()
    snapshot = _snapshot_equiv()
    prompt = _baseline_prompt()
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    samples = []
    try:
        for index in range(runs):
            attempts, tokens, cost, latency, verify = 0, 0, 0.0, 0, 0
            compile_ok, code, compile_error = False, "", ""
            first_error = ""
            report = None
            while attempts < MAX_ATTEMPTS:
                attempts += 1
                try:
                    out = adapter.raw(prompt if attempts == 1 else repair, BASELINE_SYSTEM)
                except AdapterError as error:
                    samples.append({"run": index + 1, "error": str(error), "attempts": attempts})
                    break
                code = out["code"]
                tokens += out["usage"].get("total_tokens", 0)
                cost += out["usage"].get("cost") or 0
                latency += out["latency_ms"]
                target.write_text(code, encoding="utf-8")
                began_build = time.time()
                build_report = equiv_build()
                verify += int((time.time() - began_build) * 1000)
                stage = next((item for item in build_report.get("stages", [])
                              if item["stage"] == "modern"), {})
                compile_ok = stage.get("returncode", 1) == 0
                compile_error = stage.get("stderr") or ""
                if not compile_ok and attempts == 1:
                    first_error = compile_error
                if compile_ok:
                    break
                repair = "\n".join([
                    prompt,
                    "",
                    "[上一版输出]",
                    code,
                    "",
                    "[javac 报错]",
                    compile_error[:1500],
                    "",
                    "请修正编译错误，输出完整的类源码，不要解释。",
                ])
            if attempts == 0:
                continue
            began_gate = time.time()
            report = equiv_gate() if compile_ok else None
            verify += int((time.time() - began_gate) * 1000)
            findings = [item for item in (report or {}).get("findings", [])
                        if item.get("risk") == "high"]
            samples.append({
                "run": index + 1,
                "attempts": attempts,
                "latency_ms": latency,
                "verify_ms": verify,
                "deliver_ms": latency + verify,
                "tokens": tokens,
                "cost": round(cost, 6),
                "lines": len(code.splitlines()),
                "compile_ok": compile_ok,
                "first_compile_ok": attempts == 1 and compile_ok,
                "first_compile_error": first_error.strip()[:300],
                "gate_verdict": report["verdict"] if report else "not-run",
                "gate_results": report["gate_results"] if report else {},
                "diff_count": (report or {}).get("diff", {}).get("diff_count", 0),
                "findings": [item["rule"] for item in findings],
                "first_pass": bool(report and report["verdict"] == "pass" and attempts == 1),
                "passed": bool(report and report["verdict"] == "pass"),
                "at": iso(),
            })
            log("INFO", "基线第 {}/{} 次 · {} 次尝试 · {} · 闸门 {}".format(
                index + 1, runs, attempts, "编译通过" if compile_ok else "编译失败",
                (report["verdict"].upper() if report else "N/A")))
    finally:
        target.write_bytes(original)
        _restore_equiv(snapshot)

    valid = [sample for sample in samples if "error" not in sample]
    passed = [sample for sample in valid if sample.get("passed")]
    report = {
        "arm": "baseline",
        "label": "A 基线臂 · 编译器反馈，无语义闸门",
        "runs": runs,
        "samples": samples,
        "measured": bool(valid) and status["ready"],
        "model": status.get("model"),
        "max_attempts": MAX_ATTEMPTS,
        "prompt_digest": digest,
        "prompt_chars": len(prompt),
        "compile_rate": round(len([s for s in valid if s["compile_ok"]]) / max(1, len(valid)), 3),
        "first_pass_rate": round(len([s for s in valid if s["first_pass"]]) / max(1, len(valid)), 3),
        "pass_rate": round(len(passed) / max(1, len(valid)), 3),
        "mean_attempts": round(sum(s["attempts"] for s in valid) / max(1, len(valid)), 2),
        "mean_latency_ms": round(sum(s["latency_ms"] for s in valid) / max(1, len(valid))),
        "mean_verify_ms": round(sum(s["verify_ms"] for s in valid) / max(1, len(valid))),
        "mean_deliver_ms": round(sum(s["deliver_ms"] for s in valid) / max(1, len(valid))),
        "mean_tokens": round(sum(s["tokens"] for s in valid) / max(1, len(valid))),
        "mean_cost": round(sum(s["cost"] or 0 for s in valid) / max(1, len(valid)), 6),
        "currency": "CNY",
        "note": ("同一模型、同一温度、同样最多 {} 次机会；差异只在上下文（接口面/硬约束/历史坑）"
                 "与反馈信号（javac 报错 vs 契约自检 + 五项闸门）").format(MAX_ATTEMPTS),
        "generated_at": iso(),
    }
    write_json(BASELINE, report)
    log("OK", "基线臂完成 · 编译通过率 {} · 闸门通过率 {} · 均值 {} 次尝试 / {} tokens".format(
        report["compile_rate"], report["pass_rate"], report["mean_attempts"],
        report["mean_tokens"]))
    return report


# --------------------------------------------------------------------------
# 用例生成的检出增益：手工 5 例 vs 算法 41 例
# --------------------------------------------------------------------------
def coverage_gain(runs: int = 5) -> dict:
    """同一模型连续生成 N 次，分别用人工用例与算法用例判定。

    要回答的问题只有一个：手工挑的 5 条用例，漏掉了多少次真实分歧。
    漏检 = 人工 0 差异，但算法用例有差异。
    """
    import testgen
    from generators.base import AdapterError
    from generators.llm import LLMAdapter
    import generator as generator_module

    adapter = LLMAdapter(ROOT / "model.json")
    status = adapter.status()
    if not status["ready"]:
        return {"error": "模型适配器不可用：{}".format(status["reason"])}

    target = MODERN_SRC / "ModernSettleService.java"
    original = target.read_bytes()
    cases_file = ARTIFACTS / "test" / "cases.txt"
    generated = testgen.generate(True)
    testgen.write_cases_txt(generated["cases"], cases_file)
    context = generator_module._context("ok")
    samples = []
    try:
        for index in range(runs):
            try:
                out = adapter.generate(context)
            except AdapterError as error:
                samples.append({"run": index + 1, "error": str(error)})
                continue
            target.write_text(out["code"], encoding="utf-8")
            build_report = equiv_build()
            stage = next((item for item in build_report.get("stages", [])
                          if item["stage"] == "modern"), {})
            if stage.get("returncode", 1) != 0:
                samples.append({
                    "run": index + 1, "compile_ok": False,
                    "curated_diff": 0, "generated_diff": 0,
                    "note": "编译失败，未进入差分",
                    "tokens": out.get("usage", {}).get("total_tokens", 0),
                })
                log("INFO", "增益第 {}/{} 次 · 编译失败".format(index + 1, runs))
                continue
            curated, _ = compare_cases(harness_cls(LEGACY_CLASS), harness_cls(MODERN_CLASS))
            gen, _ = compare_cases(harness_cls(LEGACY_CLASS, cases_file),
                                   harness_cls(MODERN_CLASS, cases_file))
            missed = (not curated) and bool(gen)
            samples.append({
                "run": index + 1, "compile_ok": True,
                "curated_diff": len(curated), "generated_diff": len(gen),
                "missed_by_curated": missed,
                "example": (gen[0] if gen else None),
                "tokens": out.get("usage", {}).get("total_tokens", 0),
                "at": iso(),
            })
            log("INFO", "增益第 {}/{} 次 · 人工 {} 处 / 算法 {} 处{}".format(
                index + 1, runs, len(curated), len(gen),
                " · 手工用例漏检" if missed else ""))
    finally:
        target.write_bytes(original)

    valid = [item for item in samples if "error" not in item]
    compiled = [item for item in valid if item.get("compile_ok")]
    missed = [item for item in compiled if item.get("missed_by_curated")]
    report = {
        "runs": runs,
        "samples": samples,
        "compiled": len(compiled),
        "curated_cases": 5,
        "generated_cases": generated["case_count"],
        "full_space": generated["full_space"],
        "coverage_t2": generated["coverage_t2"],
        "reduction": generated["reduction"],
        "curated_miss_count": len(missed),
        "curated_miss_rate": round(len(missed) / max(1, len(compiled)), 3),
        "measured": bool(compiled),
        "model": status.get("model"),
        "note": "漏检 = 人工 5 例 0 差异，但算法用例集发现差异",
        "generated_at": iso(),
    }
    write_json(W1 / "coverage-gain.json", report)
    log("OK", "检出增益 · {}/{} 次生成被人工用例漏检（{:.0%}）".format(
        len(missed), len(compiled), report["curated_miss_rate"]))
    return report


# --------------------------------------------------------------------------
# 平台臂：从真实运行产物推导，不手填
# --------------------------------------------------------------------------
def platform_arm() -> dict:
    state = load_state()
    spec, _source = workflow.load()
    graph = workflow.graph_payload(spec, state)
    nodes = graph["nodes"]
    gates = len([node for node in spec["nodes"] if node.get("requires")])
    approval_gap = _approval_gap(state)
    record = {
        "arm": "platform",
        "label": "W1-B 平台链路",
        "operator": "自动化",
        "started_at": state.get("started_at"),
        "ended_at": _last_trace_at(state) or iso(),
        "measured": bool(state.get("finished")),
        "machine_ms": graph["critical_ms"],
        "serial_ms": graph["serial_ms"],
        "deliver_ms": graph["critical_ms"] + approval_gap,
        "compare_ms": graph["critical_ms"],
        "approval_gap_ms": approval_gap,
        "approval_mode": "auto" if approval_gap < 2000 else "human",
        "approval_note": ("签核间隔 {} ms，属自动化跑批；真人复核耗时未计入，交付工时是下界".format(approval_gap)
                          if approval_gap < 2000 else
                          "签核间隔 {} ms，含真人复核时间".format(approval_gap)),
        "human_gates": gates,
        "manual_actions": gates + 1,
        "nodes": len(nodes),
        "attempts": sum(node["attempts"] for node in nodes),
        "rework": state.get("rework", 0),
        "first_pass": state.get("rework", 0) == 0 and state.get("gate") == "pass",
        "gate": state.get("gate"),
        "artifacts": sum(len(node["artifacts"]) for node in nodes),
        "tokens": (read_json(ARTIFACTS / "generation.json", {}) or {}).get("usage", {}).get("total_tokens", 0),
        "cost": (read_json(ARTIFACTS / "generation.json", {}) or {}).get("usage", {}).get("cost"),
        "adapter": (read_json(ARTIFACTS / "generation.json", {}) or {}).get("adapter"),
        "ai_generated": bool((read_json(ARTIFACTS / "generation.json", {}) or {}).get("ai_generated")),
        "critical_path": graph["critical_path"],
    }
    write_json(PLATFORM_ARM, record)
    return record


def _last_trace_at(state: dict) -> str:
    trace = state.get("trace") or []
    return trace[-1].get("at") if trace else ""


def _approval_gap(state: dict) -> int:
    """签核等待：计划节点完成 → 生成节点首次启动之间的真实间隔。"""
    trace = state.get("trace", [])
    plan = next((event for event in trace if event.get("node") == "plan-migration" and event.get("status") == "done"), None)
    migrate = next((event for event in trace if event.get("node") == "migrate-module"), None)
    if not plan or not migrate:
        return 0
    try:
        return max(0, int((_parse(migrate["at"]) - _parse(plan["at"])) * 1000))
    except (ValueError, KeyError):
        return 0


# --------------------------------------------------------------------------
# 基准测试：同一配置连跑 N 次，看耗时分布与一次通过率
# --------------------------------------------------------------------------
def bench(runs: int = 3, defect: bool = None, approve_auto: bool = True,
          restore: bool = True) -> dict:
    ensure_dirs()
    config = load_config()
    original = bool(config.get("inject_defect"))
    if defect is not None:
        config["inject_defect"] = defect
        write_json(CONFIG_FILE, config)

    samples = []
    for index in range(runs):
        started = time.time()
        _cli(["reset"])
        _cli(["run"])
        if approve_auto:
            _cli(["approve"])
        state = load_state()
        spec, _source = workflow.load()
        graph = workflow.graph_payload(spec, state)
        generation = read_json(ARTIFACTS / "generation.json", {}) or {}
        samples.append({
            "run": index + 1,
            "wall_ms": int((time.time() - started) * 1000),
            "critical_ms": graph["critical_ms"],
            "serial_ms": graph["serial_ms"],
            "rework": state.get("rework", 0),
            "gate": state.get("gate"),
            "finished": state.get("finished"),
            "attempts": sum(node["attempts"] for node in graph["nodes"]),
            "artifacts": sum(len(node["artifacts"]) for node in graph["nodes"]),
            "first_pass": state.get("rework", 0) == 0 and state.get("gate") == "pass",
            "defect": bool(config.get("inject_defect")),
            "adapter": generation.get("adapter"),
            "ai_generated": bool(generation.get("ai_generated")),
            "tokens": (generation.get("usage") or {}).get("total_tokens", 0),
            "at": iso(),
        })
        log("INFO", "基准第 {}/{} 次 · 关键路径 {} ms · 返工 {} 次 · 闸门 {}".format(
            index + 1, runs, graph["critical_ms"], state.get("rework", 0), state.get("gate")))

    config["inject_defect"] = original
    write_json(CONFIG_FILE, config)
    if restore:
        # 基准会把工作区改成"最后一次基准"的状态，A/B 表会指错批次。
        # 跑完按当前配置再走一次正式链路，把演示状态恢复成可交付的样子。
        try:
            _cli(["reset"])
            _cli(["run"])
            if approve_auto:
                _cli(["approve"])
            log("INFO", "基准后已重跑一次正式链路 · 演示状态已还原")
        except RuntimeError as error:
            log("WARN", "基准后还原正式链路失败 · {}".format(error))

    critical = [sample["critical_ms"] for sample in samples]
    wall = [sample["wall_ms"] for sample in samples]
    report = {
        "runs": runs, "defect": bool(samples[0]["defect"]) if samples else None,
        "samples": samples,
        "critical_ms": {"mean": round(statistics.mean(critical), 1),
                        "median": statistics.median(critical),
                        "stdev": round(statistics.pstdev(critical), 1),
                        "min": min(critical), "max": max(critical)},
        "wall_ms": {"mean": round(statistics.mean(wall), 1),
                    "stdev": round(statistics.pstdev(wall), 1)},
        "first_pass_rate": round(len([s for s in samples if s["first_pass"]]) / max(1, len(samples)), 3),
        "rework_mean": round(statistics.mean([s["rework"] for s in samples]), 2),
        "interception_rate": round(len([s for s in samples if s["rework"] > 0]) / max(1, len(samples)), 3),
        "adapter": samples[0].get("adapter") if samples else None,
        "ai_generated": bool(samples[0].get("ai_generated")) if samples else False,
        "mean_tokens": round(statistics.mean([s.get("tokens", 0) for s in samples]), 0) if samples else 0,
        "note": "同一模块同一配置连跑；关键路径为纯机器耗时，不含人工签核",
        "measured": True,
        "generated_at": iso(),
    }
    key = "on" if report["defect"] else "off"
    history = read_json(BENCH, {}) or {}
    history[key] = report
    history["latest"] = key
    write_json(BENCH, history)
    log("OK", "基准完成（注入缺陷={}）· 关键路径均值 {} ms · 标准差 {} ms · 一次通过率 {}".format(
        report["defect"], report["critical_ms"]["mean"],
        report["critical_ms"]["stdev"], report["first_pass_rate"]))
    return report


def _cli(args: list) -> int:
    """子进程失败必须立刻暴露，否则基准会安静地记下 0 ms 这种假数据。"""
    result = subprocess.run([sys.executable, "cli.py"] + args, cwd=str(ENGINE),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError("cli {} 失败({})：{}".format(
            " ".join(args), result.returncode, (result.stderr or "").strip()[-400:]))
    return result.returncode


# --------------------------------------------------------------------------
# A/B 报告：两臂合并，缺一侧就只报"待实测"
# --------------------------------------------------------------------------
def _metric(key, label, unit, arm_a, arm_b, note=""):
    a_value, a_measured = arm_a
    b_value, b_measured = arm_b
    both = a_measured and b_measured and a_value is not None and b_value is not None
    delta = delta_pct = None
    if both:
        delta = b_value - a_value
        delta_pct = round((a_value - b_value) / a_value * 100, 1) if a_value else None
    if not a_measured:
        verdict = "pending-a"
    elif not b_measured:
        verdict = "pending-b"
    else:
        verdict = "measured"
    def _round(value):
        if isinstance(value, float):
            return round(value, 4)
        return value

    return {
        "key": key, "label": label, "unit": unit,
        "arm_a": _round(a_value), "a_measured": a_measured,
        "arm_b": _round(b_value), "b_measured": b_measured,
        "delta": delta, "delta_pct": delta_pct,
        "verdict": verdict, "note": note,
    }


def _arm_a() -> dict:
    """A 臂优先用真人记录；没有真人记录就用可复现的基线实验。"""
    human = _human()
    if human.get("state") == "finished" and human.get("active_ms"):
        return {
            "kind": "human", "label": human.get("label") or "A 人工臂",
            "ready": True, "record": human,
            "deliver_ms": human.get("active_ms"),
            "first_pass": 1 if human.get("first_pass") else 0,
            "rework": human.get("rework", 0),
            "tokens": human.get("tokens", 0),
            "source": "artifacts/w1/arm-human.json",
        }
    report = read_json(BASELINE, None)
    if report and report.get("measured"):
        reasons = []
        for sample in report.get("samples", []):
            if sample.get("error"):
                continue
            if not sample.get("compile_ok"):
                first = (sample.get("compile_error") or "").splitlines()
                reasons.append(first[0] if first else "编译失败")
            elif sample.get("diff_count"):
                reasons.append("差分 {} 处".format(sample["diff_count"]))
            else:
                reasons.append("闸门通过")
        return {
            "kind": "baseline", "label": report.get("label") or "A 基线臂",
            "ready": True, "record": report,
            "deliver_ms": report.get("mean_deliver_ms") or report.get("mean_latency_ms"),
            "first_pass": report.get("first_pass_rate"),
            "rework": round((report.get("mean_attempts") or 1) - 1, 2),
            "tokens": report.get("mean_tokens", 0),
            "compile_rate": report.get("compile_rate"),
            "failures": reasons,
            "source": "artifacts/w1/baseline.json",
        }
    return {"kind": "none", "label": "A 臂未实测", "ready": False,
            "deliver_ms": None, "first_pass": None, "rework": None, "tokens": None}


def ab() -> dict:
    ensure_dirs()
    arm_a = _arm_a()
    platform = platform_arm()
    bench_report = read_json(BENCH, None)
    ready = arm_a["ready"]

    metrics = [
        _metric("deliver_ms", "机器交付耗时", "min",
                ((arm_a["deliver_ms"] or 0) / 60000 if ready else None, ready),
                (platform.get("compare_ms", platform["machine_ms"]) / 60000, platform["measured"]),
                "A=生成往返+验证；B=链路关键路径；两侧均不含人工审批"),
        _metric("first_pass", "首轮通过五项闸门", "率",
                (arm_a["first_pass"] if ready else None, ready),
                (1.0 if platform.get("first_pass") else 0.0, platform["measured"]),
                "一次生成即通过，不经返工"),
        _metric("rework", "返工次数", "次",
                (arm_a["rework"] if ready else None, ready),
                (platform["rework"], platform["measured"]),
                "以闸门不通过后的修复轮次计"),
        _metric("tokens", "Token 用量", "tokens",
                (arm_a["tokens"] if ready else None, ready),
                (platform["tokens"], platform["measured"]),
                "含自检修复重试的累计用量"),
    ]
    # 两臂必须同源：A 是模型基线，B 却跑成了规则适配器，配对出来的数字毫无意义
    pairing_error = ""
    if arm_a["kind"] == "baseline" and not platform.get("ai_generated"):
        pairing_error = ("A 臂为模型基线（{}），B 臂当前为 {}（非模型生成），两臂不同源，不予配对"
                         .format((arm_a.get("record") or {}).get("model") or "模型",
                                 platform.get("adapter") or "未知适配器"))
        for metric in metrics:
            if metric["verdict"] == "measured":
                metric["verdict"] = "invalid-pairing"
                metric["delta"] = None
                metric["delta_pct"] = None

    paired = [metric for metric in metrics if metric["verdict"] == "measured"]
    report = {
        "module": "OrderSettleService",
        "acceptance": "五项闸门 + 存量行为等价（两臂同一验收标准）",
        "arm_a": arm_a,
        "arm_b": platform,
        "arm_a_kind": arm_a["kind"],
        "platform_arm": platform,
        "metrics": metrics,
        "paired_metrics": len(paired),
        "pairing_error": pairing_error,
        "headline": (_headline(paired, ready, arm_a) if not pairing_error
                     else {"available": False, "text": pairing_error,
                           "next": "把生成适配层切回 llm 后重跑链路，或重跑 baseline",
                           "platform_ms": round(platform.get("compare_ms", 0) / 60000, 2)}),
        "bench": {
            "on": (bench_report or {}).get("on"),
            "off": (bench_report or {}).get("off"),
        } if isinstance(bench_report, dict) else None,
        "doc_baseline": dict(DOC_BASELINE, measured=False),
        "claim_policy": "两臂都实测才计算差值；A 臂真人记录缺失时用可复现的基线实验，并注明是基线不是人工",
        "generated_at": iso(),
    }
    write_json(AB_REPORT, report)
    write_json(ARTIFACTS / "report" / "efficiency-ab.json", report)
    if pairing_error:
        log("WARN", "A/B 未配对 · {}".format(pairing_error))
    else:
        log("OK" if ready else "HITL",
            "A/B 报告已生成 · A={} · 配对指标 {} 项".format(
                arm_a["kind"], len(paired)))
    return report


def _headline(paired: list, ready: bool, arm_a: dict) -> dict:
    platform = read_json(PLATFORM_ARM, {}) or {}
    platform_min = round(platform.get("compare_ms", platform.get("deliver_ms", 0)) / 60000, 2)
    if not ready:
        return {
            "available": False,
            "text": "A 臂未实测，差值暂不宣称",
            "next": "python efficiency.py baseline --runs 3",
            "platform_ms": platform_min,
        }
    first = next((m for m in paired if m["key"] == "first_pass"), None)
    rework = next((m for m in paired if m["key"] == "rework"), None)
    pieces = []
    if first:
        pieces.append("首轮闸门通过率 {:.0%} → {:.0%}".format(first["arm_a"], first["arm_b"]))
    if rework:
        pieces.append("返工 {:.1f} 次 → {} 次".format(rework["arm_a"], rework["arm_b"]))
    kind = "人工臂" if arm_a["kind"] == "human" else "基线臂"
    text = "；".join(pieces) if pieces else "两臂已实测"
    return {
        "available": True,
        "text": "{} vs 平台：{}".format(kind, text),
        "arm_a_kind": arm_a["kind"],
        "first_pass_delta": round(first["arm_b"] - first["arm_a"], 3) if first else None,
        "platform_ms": platform_min,
        "next": "",
    }


# --------------------------------------------------------------------------
# 实验协议
# --------------------------------------------------------------------------
PROTOCOL = """# W1 效率对照实验协议

## 1. 要证明什么
同一模块（OrderSettleService，存量 1 个类 / 2 个公共方法 / 约 80 行有效逻辑）
在**同一验收标准**（五项闸门全绿 + 存量行为等价）下，不同工作方式的差异。

只证明这一件事，且**必须说清是哪一种对照**：
- A1 基线臂 → 证明「编排带来的提升」；
- A2 人工臂 → 才能证明「人效提升」。

两者不可混用，不可互相代言。

## 2. 三个臂
| | A1 基线臂（默认，自动可复现） | A2 人工臂（可选，需真人） | B 平台臂 |
|---|---|---|---|
| 执行者 | 同一模型 | 真实工程师 | 同一模型 + 编排 |
| 上下文 | 存量代码 + 业务规则 | 全部资料 + IDE | 代码图谱接口面 + 硬约束 + 历史坑 + 映射表 |
| 反馈 | javac 报错（最多 2 次机会） | 编译器 + 自己的判断 | 契约自检 + 五项闸门（失败走回边） |
| 计时 | 生成往返 + 验证耗时 | 人工打点 | 节点耗时 + 签核等待 |

> A1 的两次机会与 B 的对等：都用同一模型、同一温度、同一份存量代码。

## 3. 计时口径
```
交付耗时 = 生成/编码往返 + 达到验收标准所需的机器验证时间 + 返工
```
A1 必须把「跑五项闸门的时间」算进去，否则等于让基线白嫖验收。
人工臂的暂停（吃饭/会议）用 pause/resume 剔除，联调耗时单列。

## 4. 记录方法
```bash
# A1 基线臂（需要模型密钥）
python engine/efficiency.py baseline --runs 5

# A2 人工臂（真人）
python engine/efficiency.py human-start --label "W1-A 人工" --operator 张三
python engine/efficiency.py human-mark --note "读完存量实现" --steps 6
python engine/efficiency.py human-pause
python engine/efficiency.py human-resume
python engine/efficiency.py human-stop --rework 3 --first-pass no --integration-min 18

# B 平台臂（自动）
python engine/efficiency.py bench --runs 3 --defect off

# 合并（有真人记录时自动优先用真人臂）
python engine/efficiency.py ab
```

## 5. 偏差控制
- 同一台机器、同一 JDK、同一份样例工程；
- A1 与 B 必须同一模型、同一温度，只允许上下文与反馈信号不同；
- A2 期间不打开 `hub/artifacts/`，避免参考平台产物；
- 两臂都从干净工作区开始；人工臂中途放弃要如实记录放弃点。

## 6. 判读规则（重要）
1. 任一臂未实测 → 只报「待实测」，**不计算差值**；
2. 用 A1 得到的数字，只能表述为「相比无编排的裸生成」，**不得表述为人效提升**；
3. 平台臂机器耗时不等于交付工时，必须加上人工签核与复核耗时；
4. 文档基线（80 人时 → 6.5 人时）**永远标 measured=false**，只作参照；
5. 人工臂样本 <1 或中途换人 → 标注「单样本，仅供参考」。

## 7. 需要留存的原始记录
- `artifacts/w1/baseline.json`（A1：逐次尝试、首轮编译报错、token、闸门结论）
- `artifacts/w1/arm-human.json`（A2：事件流与暂停区间）
- `artifacts/w1/arm-platform.json`（B：节点级耗时与关键路径）
- `artifacts/w1/bench.json`（N 次连跑的分布）
- `artifacts/w1/ab-report.json`（配对结果、两臂类型与判读规则）

协议版本 v2 · 生成于 {stamp}
"""


def protocol() -> dict:
    ensure_dirs()
    path = W1 / "protocol.md"
    write_text(path, PROTOCOL.format(stamp=iso()))
    return {"protocol": "artifacts/w1/protocol.md"}


def show() -> dict:
    human = _human()
    platform = read_json(PLATFORM_ARM, None)
    bench_report = read_json(BENCH, None)
    return {
        "human_arm": {"ready": human.get("state") == "finished",
                      "state": human.get("state", "absent"),
                      "active_ms": human.get("active_ms")},
        "platform_arm": bool(platform),
        "bench": {key: {"runs": item["runs"], "mean_ms": item["critical_ms"]["mean"],
                        "first_pass_rate": item["first_pass_rate"]}
                  for key, item in (bench_report or {}).items() if key != "latest"}
        if bench_report else None,
        "baseline": read_json(BASELINE, None) is not None,
        "ab_report": (AB_REPORT).exists(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="效率证明 · A/B 对照与基准测试")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("human-start")
    start.add_argument("--label", default="W1-A 人工实现")
    start.add_argument("--operator", default="匿名")
    mark = sub.add_parser("human-mark")
    mark.add_argument("--note", required=True)
    mark.add_argument("--steps", type=int, default=0)
    sub.add_parser("human-pause")
    sub.add_parser("human-resume")
    stop = sub.add_parser("human-stop")
    stop.add_argument("--rework", type=int, default=0)
    stop.add_argument("--first-pass", choices=["yes", "no"], default="no")
    stop.add_argument("--integration-min", type=float, default=0)
    stop.add_argument("--notes", default="")
    imp = sub.add_parser("human-import")
    imp.add_argument("--file", required=True)
    bench_parser = sub.add_parser("bench")
    bench_parser.add_argument("--runs", type=int, default=3)
    bench_parser.add_argument("--defect", choices=["on", "off"])
    baseline_parser = sub.add_parser("baseline")
    baseline_parser.add_argument("--runs", type=int, default=3)
    gain_parser = sub.add_parser("coverage-gain")
    gain_parser.add_argument("--runs", type=int, default=5)
    sub.add_parser("ab")
    sub.add_parser("protocol")
    sub.add_parser("show")
    args = parser.parse_args()

    ensure_dirs()
    if args.command == "human-start":
        print(json.dumps(human_start(args.label, args.operator), ensure_ascii=False))
    elif args.command == "human-mark":
        print(json.dumps(human_mark(args.note, args.steps), ensure_ascii=False))
    elif args.command == "human-pause":
        print(json.dumps(human_pause("pause"), ensure_ascii=False))
    elif args.command == "human-resume":
        print(json.dumps(human_pause("resume"), ensure_ascii=False))
    elif args.command == "human-stop":
        print(json.dumps(human_stop(args.rework, args.first_pass,
                                    args.integration_min, args.notes), ensure_ascii=False))
    elif args.command == "human-import":
        print(json.dumps(human_import(Path(args.file)), ensure_ascii=False))
    elif args.command == "bench":
        defect = None if args.defect is None else args.defect == "on"
        report = bench(args.runs, defect)
        print(json.dumps({"runs": report["runs"], "critical_ms": report["critical_ms"],
                          "first_pass_rate": report["first_pass_rate"]}, ensure_ascii=False))
    elif args.command == "baseline":
        report = baseline(args.runs)
        print(json.dumps({key: report.get(key) for key in
                          ("runs", "compile_rate", "first_pass_rate", "mean_latency_ms",
                           "mean_tokens", "mean_cost", "error") if key in report},
                         ensure_ascii=False))
    elif args.command == "coverage-gain":
        report = coverage_gain(args.runs)
        print(json.dumps({key: report.get(key) for key in
                          ("runs", "compiled", "curated_cases", "generated_cases",
                           "coverage_t2", "curated_miss_count", "curated_miss_rate", "error")
                          if key in report}, ensure_ascii=False))
    elif args.command == "ab":
        report = ab()
        print(json.dumps({"paired": report["paired_metrics"],
                          "headline": report["headline"]}, ensure_ascii=False))
    elif args.command == "protocol":
        print(json.dumps(protocol(), ensure_ascii=False))
    elif args.command == "show":
        print(json.dumps(show(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
