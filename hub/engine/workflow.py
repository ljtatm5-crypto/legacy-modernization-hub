"""工作流 DAG 引擎：由 hub/workflow.yaml 驱动，支持依赖、条件、审批、并行、失败回边。

设计取舍：
- 不引入第三方库，`when` 表达式用 ast 白名单解释，禁掉函数调用与属性访问；
- 状态仍由主线程串行落盘，worker 只拿状态快照，避免并行写坏 state.json；
- 工作流文件不合法时回退内置 DAG 并如实记录来源，不让一个手滑的缩进毁掉整条链路。
"""
from __future__ import annotations

import ast
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from hublib import ARTIFACTS, WORKFLOW_FILE, iso, iso_ms, log, read_json, write_json

DEFAULT_WORKFLOW = {
    "version": 1,
    "name": "legacy-modernization",
    "module": "OrderSettleService",
    "concurrency": 3,
    "nodes": [
        {"id": "scan-arch", "skill": "/scan-arch", "title": "架构逆向扫描",
         "handler": "scan_arch", "depends_on": []},
        {"id": "extract-rules", "skill": "/extract-rules", "title": "业务规则抽取",
         "handler": "extract_rules", "depends_on": ["scan-arch"]},
        {"id": "plan-migration", "skill": "/plan-migration", "title": "迁移计划与风险清单",
         "handler": "plan_migration", "depends_on": ["extract-rules"]},
        {"id": "migrate-module", "skill": "/migrate-module", "title": "按方法粒度生成",
         "handler": "migrate_module", "depends_on": ["plan-migration"], "requires": "approved"},
        {"id": "dependency-audit", "skill": "/audit-deps", "title": "依赖与 API 兼容审计",
         "handler": "audit_deps", "depends_on": ["migrate-module"], "optional": True},
        {"id": "api-surface", "skill": "/api-surface", "title": "公共 API 面比对",
         "handler": "api_surface", "depends_on": ["migrate-module"]},
        {"id": "equiv-check", "skill": "/equiv-check", "title": "行为等价性闸门",
         "handler": "equiv_check", "depends_on": ["migrate-module"],
         "on_fail": {"remediate": "tx_boundary_patch", "goto": "migrate-module", "max": 1}},
        {"id": "gen-regression", "skill": "/gen-regression", "title": "回归用例生成",
         "handler": "gen_regression", "depends_on": ["equiv-check"], "when": "gate == 'pass'"},
        {"id": "report-delta", "skill": "/report-delta", "title": "差异报告与复盘",
         "handler": "report_delta",
         "depends_on": ["gen-regression", "dependency-audit", "api-surface"]},
    ],
}

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
)


class WorkflowError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise WorkflowError(message)


def evaluate(expression: str, context: dict):
    """安全解释 when 表达式：只允许布尔/比较运算与常量，禁止调用与属性访问。"""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise WorkflowError("when 表达式语法错误：{} ({})".format(expression, error))
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise WorkflowError("when 表达式不允许 {}：{}".format(type(node).__name__, expression))
    return eval(compile(tree, "<when>", "eval"), {"__builtins__": {}}, dict(context))


def normalize(spec: dict) -> dict:
    _require(isinstance(spec, dict), "工作流定义必须是映射")
    raw_nodes = spec.get("nodes")
    _require(isinstance(raw_nodes, list) and raw_nodes, "工作流缺少 nodes 列表")
    nodes, seen = [], set()
    for index, raw in enumerate(raw_nodes):
        _require(isinstance(raw, dict), "nodes[{}] 必须是映射".format(index))
        node_id = raw.get("id")
        _require(bool(node_id), "nodes[{}] 缺少 id".format(index))
        _require(node_id not in seen, "节点 id 重复：{}".format(node_id))
        seen.add(node_id)
        depends = raw.get("depends_on") or []
        _require(isinstance(depends, list), "{} 的 depends_on 必须是列表".format(node_id))
        nodes.append({
            "id": str(node_id),
            "skill": str(raw.get("skill") or "/" + str(node_id)),
            "title": str(raw.get("title") or node_id),
            "handler": str(raw.get("handler") or str(node_id).replace("-", "_")),
            "depends_on": [str(dep) for dep in depends],
            "when": raw.get("when"),
            "requires": raw.get("requires"),
            "approval": bool(raw.get("approval")),
            "optional": bool(raw.get("optional")),
            "on_fail": raw.get("on_fail") if isinstance(raw.get("on_fail"), dict) else None,
        })
    for node in nodes:
        for dep in node["depends_on"]:
            _require(dep in seen, "{} 依赖了不存在的节点 {}".format(node["id"], dep))
        _require(node["id"] not in node["depends_on"], "{} 依赖了自己".format(node["id"]))
    result = dict(spec)
    result["nodes"] = nodes
    result["concurrency"] = max(1, int(spec.get("concurrency") or 3))
    return result


def load(path: Path = None) -> tuple:
    """返回 (spec, source)。文件损坏时回退内置 DAG，source 说明原因。"""
    target = Path(path or WORKFLOW_FILE)
    if not target.exists():
        return normalize(copy.deepcopy(DEFAULT_WORKFLOW)), {"kind": "builtin", "reason": "未找到 workflow.yaml"}
    try:
        import miniyaml
        spec = miniyaml.load(target)
        return normalize(spec), {"kind": "file", "path": "hub/workflow.yaml"}
    except Exception as error:  # 解析失败必须可解释，不静默
        return normalize(copy.deepcopy(DEFAULT_WORKFLOW)), {
            "kind": "builtin",
            "reason": "{}: {}".format(type(error).__name__, error),
        }


def order_of(spec: dict) -> list:
    """Kahn 拓扑排序，同时检测环。"""
    nodes = {node["id"]: node for node in spec["nodes"]}
    indegree = {node["id"]: len(node["depends_on"]) for node in spec["nodes"]}
    queue = [node["id"] for node in spec["nodes"] if indegree[node["id"]] == 0]
    ordered = []
    while queue:
        current = queue.pop(0)
        ordered.append(current)
        for node in spec["nodes"]:
            if current in node["depends_on"]:
                indegree[node["id"]] -= 1
                if indegree[node["id"]] == 0:
                    queue.append(node["id"])
    _require(len(ordered) == len(nodes), "工作流存在环：{}".format(
        ", ".join(sorted(set(nodes) - set(ordered)))))
    return ordered


def layers_of(spec: dict, order: list) -> dict:
    nodes = {node["id"]: node for node in spec["nodes"]}
    layer = {}
    for node_id in order:
        deps = nodes[node_id]["depends_on"]
        layer[node_id] = 0 if not deps else max(layer[dep] for dep in deps) + 1
    return layer


def descendants(spec: dict, node_id: str) -> list:
    nodes = spec["nodes"]
    found, stack = set(), [node_id]
    while stack:
        current = stack.pop()
        for node in nodes:
            if current in node["depends_on"] and node["id"] not in found:
                found.add(node["id"])
                stack.append(node["id"])
    return sorted(found)


def _critical_path(spec: dict, durations: dict) -> dict:
    nodes = {node["id"]: node for node in spec["nodes"]}
    best, back = {}, {}
    for node_id in order_of(spec):
        options = [(best.get(dep, 0), dep) for dep in nodes[node_id]["depends_on"]] or [(0, None)]
        weight, parent = max(options)
        best[node_id] = weight + durations.get(node_id, 0)
        back[node_id] = parent
    if not best:
        return {"path": [], "ms": 0}
    tail = max(best, key=lambda key: best[key])
    path, cursor = [], tail
    while cursor:
        path.append(cursor)
        cursor = back[cursor]
    return {"path": list(reversed(path)), "ms": best[tail]}


def graph_payload(spec: dict, state: dict) -> dict:
    order = order_of(spec)
    layer = layers_of(spec, order)
    nodes = state.get("nodes", {})
    durations = {node_id: nodes.get(node_id, {}).get("duration_ms", 0) for node_id in order}
    critical = _critical_path(spec, durations)
    serial = sum(durations.values())
    projected = [{
        "id": node["id"], "skill": node["skill"], "title": node["title"],
        "layer": layer[node["id"]], "depends_on": node["depends_on"],
        "requires": node["requires"], "optional": node["optional"],
        "when": node["when"], "on_fail": node["on_fail"],
        "status": nodes.get(node["id"], {}).get("status", "pending"),
        "attempts": nodes.get(node["id"], {}).get("attempts", 0),
        "duration_ms": durations[node["id"]],
        "artifacts": nodes.get(node["id"], {}).get("artifacts", []),
        "thread": nodes.get(node["id"], {}).get("thread", ""),
        "regenerated": nodes.get(node["id"], {}).get("regenerated", False),
        "note": nodes.get(node["id"], {}).get("note", ""),
    } for node in spec["nodes"]]
    widths = {}
    for item in projected:
        widths[item["layer"]] = widths.get(item["layer"], 0) + 1
    on_path = set(critical["path"])
    off_critical = sum(duration for node_id, duration in durations.items() if node_id not in on_path)
    return {
        "name": spec.get("name"), "module": spec.get("module"),
        "concurrency": spec["concurrency"],
        "source": state.get("workflow", {}).get("source"),
        "nodes": projected,
        "edges": [[dep, node["id"]] for node in spec["nodes"] for dep in node["depends_on"]],
        "layer_count": len(widths),
        "parallel_width": max(widths.values()) if widths else 0,
        "layers": [sorted([item["id"] for item in projected if item["layer"] == index])
                   for index in range(len(widths))],
        "critical_path": critical["path"], "critical_ms": critical["ms"],
        "serial_ms": serial, "off_critical_ms": off_critical,
        "parallel_gain_ms": max(0, serial - critical["ms"]),
        "trace": state.get("trace", []),
    }


def _sync_steps(spec: dict, state: dict) -> None:
    """把 DAG 节点状态投影成前端使用的 steps 列表（保持既有渲染契约）。"""
    order = order_of(spec)
    layer = layers_of(spec, order)
    by_id = {node["id"]: node for node in spec["nodes"]}
    state["steps"] = [{
        "id": by_id[node_id]["skill"],
        "node_id": node_id,
        "name": by_id[node_id]["title"],
        "layer": layer[node_id],
        "depends_on": by_id[node_id]["depends_on"],
        "status": state["nodes"].get(node_id, {}).get("status", "pending"),
        "artifacts": state["nodes"].get(node_id, {}).get("artifacts", []),
        "duration_ms": state["nodes"].get(node_id, {}).get("duration_ms", 0),
        "attempts": state["nodes"].get(node_id, {}).get("attempts", 0),
        "regenerated": state["nodes"].get(node_id, {}).get("regenerated", False),
        "note": state["nodes"].get(node_id, {}).get("note", ""),
    } for node_id in order]


class Runner:
    """按 DAG 调度执行器。handler 签名：handler(state_snapshot) -> dict。"""

    def __init__(self, spec: dict, handlers: dict, remediations: dict, saver):
        self.spec = spec
        self.handlers = handlers
        self.remediations = remediations
        self.save = saver
        self.order = order_of(spec)
        self.by_id = {node["id"]: node for node in spec["nodes"]}

    def _context(self, state: dict) -> dict:
        return {
            "approved": bool(state.get("plan_approved")),
            "gate": state.get("gate"),
            "rework": state.get("rework", 0),
            "fixed": bool(state.get("fixed")),
            "defect": bool(state.get("inject_defect")),
        }

    def _ensure_nodes(self, state: dict) -> None:
        state.setdefault("nodes", {})
        for node_id in self.order:
            entry = state["nodes"].setdefault(node_id, {
                "status": "pending", "attempts": 0, "artifacts": [], "duration_ms": 0,
            })
            if entry.get("status") in ("running", "blocked"):  # 上次进程被中断 / 上游已修复
                entry["status"] = "pending"
            if entry.get("status") == "waiting" and state.get("plan_approved"):
                entry["status"] = "pending"

    def _trace(self, state: dict, **event) -> None:
        state.setdefault("trace", []).append(event)

    def run(self) -> str:
        from hublib import load_state
        state = load_state()
        self._ensure_nodes(state)
        reused = sum(1 for node_id in self.order
                     if state["nodes"][node_id]["status"] == "done")
        state["skipped_steps"] = reused
        if reused:
            log("INFO", "断点续跑 · 复用 {} 个已完成节点的产物".format(reused))

        while True:
            pending = [node_id for node_id in self.order
                       if state["nodes"][node_id]["status"] == "pending"]
            if not pending:
                break
            ready = [node_id for node_id in pending
                     if all(state["nodes"][dep]["status"] in ("done", "skipped")
                            for dep in self.by_id[node_id]["depends_on"])]
            if not ready:
                blocked = [node_id for node_id in pending
                           if any(state["nodes"][dep]["status"] == "failed"
                                  for dep in self.by_id[node_id]["depends_on"])]
                for node_id in blocked:
                    state["nodes"][node_id]["status"] = "blocked"
                _sync_steps(self.spec, state)
                self.save(state)
                log("ERR", "上游失败导致 {} 个节点无法执行 · 链路终止".format(len(blocked)))
                return "blocked"

            waiting = [node_id for node_id in ready
                       if self.by_id[node_id].get("requires") == "approved"
                       and not state.get("plan_approved")]
            if waiting:
                for node_id in waiting:
                    state["nodes"][node_id]["status"] = "waiting"
                state["checkpoint"] = waiting[0]
                _sync_steps(self.spec, state)
                self.save(state)
                log("HITL", "链路暂停 · {} 需人工签核后才能执行".format(
                    self.by_id[waiting[0]]["skill"]))
                return "waiting-approval"
            ready = [node_id for node_id in ready if node_id not in waiting]

            skipped = []
            for node_id in list(ready):
                condition = self.by_id[node_id].get("when")
                if condition and not evaluate(condition, self._context(state)):
                    state["nodes"][node_id]["status"] = "skipped"
                    state["nodes"][node_id]["note"] = "条件不满足：{}".format(condition)
                    skipped.append(node_id)
                    ready.remove(node_id)
                    log("INFO", "{} 条件不满足跳过 · {}".format(
                        self.by_id[node_id]["skill"], condition))
            if skipped:
                _sync_steps(self.spec, state)
                self.save(state)
            if not ready:
                continue

            for node_id in ready:
                state["nodes"][node_id]["status"] = "running"
                state["nodes"][node_id]["attempts"] = state["nodes"][node_id].get("attempts", 0) + 1
            state["checkpoint"] = ready[0]
            _sync_steps(self.spec, state)
            self.save(state)
            for node_id in ready:
                log("INFO", "▶ {} 启动 · 第 {} 次尝试{}".format(
                    self.by_id[node_id]["skill"],
                    state["nodes"][node_id]["attempts"],
                    " · 并行" if len(ready) > 1 else ""))

            outcomes = self._dispatch(ready, state)
            failures = []
            for node_id in ready:
                outcome = outcomes[node_id]
                entry = state["nodes"][node_id]
                entry["duration_ms"] = outcome["duration_ms"]
                entry["artifacts"] = outcome["artifacts"]
                entry["thread"] = outcome["thread"]
                if outcome.get("context"):
                    state.update(outcome["context"])
                entry["note"] = outcome.get("detail", "")
                if outcome["status"] == "fail":
                    entry["status"] = "fail"
                    failures.append((node_id, outcome))
                    self._trace(state, node=node_id, attempt=entry["attempts"], status="fail",
                                duration_ms=outcome["duration_ms"], thread=outcome["thread"],
                                detail=outcome.get("detail", ""), at=iso_ms())
                else:
                    entry["status"] = "done"
                    self._trace(state, node=node_id, attempt=entry["attempts"], status="done",
                                duration_ms=outcome["duration_ms"], thread=outcome["thread"],
                                detail=outcome.get("detail", ""), at=iso_ms())
                    log("OK", "{} 完成 · {} 个产物 · {} ms".format(
                        self.by_id[node_id]["skill"], len(outcome["artifacts"]),
                        outcome["duration_ms"]))
            _sync_steps(self.spec, state)
            self.save(state)

            if failures:
                node_id, outcome = failures[0]
                rule = self.by_id[node_id].get("on_fail")
                attempts = state["nodes"][node_id]["attempts"]
                if not rule or attempts > int(rule.get("max", 0)):
                    state["nodes"][node_id]["status"] = "failed"
                    _sync_steps(self.spec, state)
                    self.save(state)
                    log("ERR", "节点判定不通过且无补救规则 · 链路终止 · {}".format(
                        outcome.get("detail", "")))
                    return "failed"
                state["rework"] = state.get("rework", 0) + 1
                action = rule.get("remediate")
                log("WARN", "{} 判定不通过 · 触发失败回边 {} → {}".format(
                    self.by_id[node_id]["skill"], action, self.by_id[rule["goto"]]["skill"]))
                if action in self.remediations:
                    detail = self.remediations[action](state)
                    log("INFO", "自动补救 {} 已执行 · {}".format(action, detail))
                for target in [rule["goto"]] + descendants(self.spec, rule["goto"]):
                    if target == node_id:
                        continue
                    entry = state["nodes"][target]
                    entry["status"] = "pending"
                    if target == rule["goto"]:
                        entry["regenerated"] = True
                state["nodes"][node_id]["status"] = "pending"
                state["nodes"][node_id]["note"] = "待复检"
                _sync_steps(self.spec, state)
                self.save(state)

        state["finished"] = True
        state["checkpoint"] = "finished"
        graph = graph_payload(self.spec, state)
        state["workflow"] = dict(state.get("workflow", {}))
        state["workflow"]["critical_path"] = graph["critical_path"]
        _sync_steps(self.spec, state)
        self.save(state)
        write_json(ARTIFACTS / "workflow" / "graph.json", graph)
        # /report-delta 在自身结束时还拿不到自己的耗时，这里用最终图谱校正一次
        summary_path = ARTIFACTS / "report" / "delta-summary.json"
        summary = read_json(summary_path)
        if summary:
            summary["workflow"] = {
                "name": graph["name"], "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]), "concurrency": graph["concurrency"],
                "layers": graph["layer_count"], "parallel_width": graph["parallel_width"],
                "critical_path": graph["critical_path"], "critical_ms": graph["critical_ms"],
                "serial_ms": graph["serial_ms"], "off_critical_ms": graph["off_critical_ms"],
            }
            summary["efficiency_mechanisms"] = _mechanisms_with(summary, graph)
            write_json(summary_path, summary)
        # 链路一结束就刷新平台臂与 A/B，避免界面上挂着上一批次的数字
        try:
            import efficiency
            efficiency.platform_arm()
            efficiency.ab()
        except Exception as error:  # 报表刷新失败不能把链路判成失败
            log("WARN", "A/B 自动刷新失败 · {}".format(error))
        log("OK", "工作流完成 · {} 个节点全部落定 · 轨迹已导出 workflow/graph.json".format(
            len(self.order)))
        return "finished"


    def _dispatch(self, node_ids: list, state: dict) -> dict:
        snapshot = copy.deepcopy(state)
        results = {}
        concurrency = min(self.spec["concurrency"], len(node_ids))
        if concurrency <= 1:
            for node_id in node_ids:
                results[node_id] = self._invoke(node_id, snapshot, None)
            return results
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                node_id: pool.submit(self._invoke, node_id, snapshot, "pool-" + str(index + 1))
                for index, node_id in enumerate(node_ids)
            }
            for node_id, future in futures.items():
                results[node_id] = future.result()
        return results

    def _invoke(self, node_id: str, snapshot: dict, thread: str) -> dict:
        import threading
        name = thread or threading.current_thread().name
        handler = self.handlers.get(self.by_id[node_id]["handler"])
        began = time.time()
        if handler is None:
            return {"status": "fail", "artifacts": [], "duration_ms": 0, "thread": name,
                    "detail": "未注册 handler: " + self.by_id[node_id]["handler"]}
        outcome = handler(copy.deepcopy(snapshot))
        elapsed = int((time.time() - began) * 1000)
        if isinstance(outcome, list):  # 兼容旧签名
            outcome = {"artifacts": outcome}
        return {
            "status": outcome.get("status", "pass"),
            "artifacts": outcome.get("artifacts", []),
            "detail": outcome.get("detail", ""),
            "context": outcome.get("context", {}),
            "duration_ms": elapsed,
            "thread": name,
        }


def _mechanisms_with(summary: dict, graph: dict) -> list:
    items = list(summary.get("efficiency_mechanisms") or [])
    for item in items:
        if item.get("id") == "parallel":
            item["value"] = "{} 节点 / {} 并发 / {} 层".format(
                len(graph["nodes"]), graph["concurrency"], graph["layer_count"])
            item["detail"] = "旁支 {} ms 完全被关键路径覆盖；关键路径 {} ms，串行需 {} ms".format(
                graph["off_critical_ms"], graph["critical_ms"], graph["serial_ms"])
        if item.get("id") == "machine":
            item["value"] = "{} s".format(round(graph["critical_ms"] / 1000, 1))
    return items
