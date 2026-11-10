"""Legacy Modernization Hub · 零依赖 stdio MCP Server。

协议：JSON-RPC 2.0，newline-delimited JSON over stdin/stdout。
所有工具都读真实产物或触发真实执行，不返回硬编码结果。

启动：python hub/mcp-server/server.py
可由任意 MCP 客户端以 stdio 方式接入（码道 / Cline / Claude Desktop 等）。
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
ARTIFACTS = ROOT / "artifacts"
PROJECT = ROOT / "sample-project"
sys.path.insert(0, str(ENGINE))

from hublib import read_json  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "legacy-modernization-hub", "version": "1.0.0"}


def _text(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _exists(name: str):
    path = ARTIFACTS / name
    if not path.exists():
        return {"error": "artifact not found: {}".format(name),
                "hint": "先运行 python hub/engine/cli.py run --restart"}
    return read_json(path, {"raw": path.read_text(encoding="utf-8")})


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------
def tool_find_callers(args: dict):
    graph = _exists("graph/code-graph.json")
    if "error" in graph:
        return graph
    method = args.get("method", "settle")
    return {
        "method": method,
        "calls": [call for call in graph.get("calls", [])
                  if method in call["file"] or not method],
        "declared_in": [item for item in graph.get("methods", [])
                        if item["method"] == method],
        "scanned_files": graph.get("scanned_files", []),
    }


def tool_get_table_lineage(args: dict):
    return _exists("graph/table-lineage.json")


def tool_get_tx_boundary(args: dict):
    path = ARTIFACTS / "graph" / "tx-boundary.yaml"
    if not path.exists():
        return {"error": "tx-boundary.yaml not found", "hint": "先运行 /scan-arch"}
    return {"file": "artifacts/graph/tx-boundary.yaml",
            "content": path.read_text(encoding="utf-8")}


def tool_get_rules(args: dict):
    path = PROJECT / "rules" / "domain-settle.json"
    data = read_json(path, {})
    confidence = args.get("min_confidence", 0)
    data["rules"] = [rule for rule in data.get("rules", [])
                     if rule.get("confidence", 0) >= confidence]
    return data


def tool_get_plan(args: dict):
    return _exists("plan/risk-register.json")


def tool_get_gate_report(args: dict):
    which = args.get("attempt", "final")
    name = "equiv/gate-first-attempt.json" if which == "first" else "equiv/gate-report.json"
    return {"attempt": which, "report": _exists(name)}


def tool_get_diff(args: dict):
    which = args.get("attempt", "final")
    name = ("equiv/dual-run-diff-first-attempt.json" if which == "first"
            else "equiv/dual-run-diff.json")
    return {"attempt": which, "diff": _exists(name)}


def tool_list_artifacts(args: dict):
    if not ARTIFACTS.exists():
        return {"files": [], "count": 0}
    files = []
    for path in sorted(ARTIFACTS.rglob("*")):
        if path.is_file():
            files.append({
                "path": "artifacts/" + str(path.relative_to(ARTIFACTS)).replace("\\", "/"),
                "bytes": path.stat().st_size,
            })
    return {"count": len(files), "files": files}


def chain_running() -> bool:
    """CLI 链路执行期间禁止外部工具并发写同一批产物；陈旧状态自动过期。"""
    state_path = ARTIFACTS / "state.json"
    if not state_path.exists():
        return False
    if time.time() - state_path.stat().st_mtime > 300:
        return False
    state = read_json(state_path, {})
    return any(step.get("status") == "running" for step in state.get("steps", []))


def tool_run_build(args: dict):
    if chain_running():
        return {"error": "CLI 链路正在执行，避免并发覆盖产物", "hint": "稍后重试"}
    from equiv import build
    print("[mcp] run_build invoked", file=sys.stderr, flush=True)
    report = build()
    return {"ok": report["ok"], "stages": report["stages"],
            "duration_ms": report["duration_ms"]}


def tool_run_equiv_check(args: dict):
    if chain_running():
        return {"error": "CLI 链路正在执行，避免并发覆盖产物", "hint": "稍后重试"}
    from equiv import gate
    print("[mcp] run_equiv_check invoked", file=sys.stderr, flush=True)
    report = gate()
    return {"verdict": report["verdict"], "gate_results": report["gate_results"],
            "diff_count": report.get("diff", {}).get("diff_count", 0),
            "findings": [item["rule"] for item in report.get("findings", [])]}


def tool_get_source(args: dict):
    which = args.get("which", "modern")
    base = (PROJECT / "modern" / "src" / "com" / "demo" / "settle" if which == "modern"
            else PROJECT / "legacy" / "src" / "com" / "demo" / "settle")
    name = "ModernSettleService.java" if which == "modern" else "LegacySettleService.java"
    path = base / name
    if not path.exists():
        return {"error": "source not found: {}".format(path)}
    return {"which": which, "file": name, "content": path.read_text(encoding="utf-8")}


TOOLS = {
    "find_callers": {
        "description": "从真实代码图谱中查询方法调用与声明位置",
        "schema": {"type": "object", "properties": {"method": {"type": "string"}}},
        "handler": tool_find_callers,
    },
    "get_table_lineage": {
        "description": "返回数据表血缘与写入操作",
        "schema": {"type": "object", "properties": {}},
        "handler": tool_get_table_lineage,
    },
    "get_tx_boundary": {
        "description": "返回事务边界声明与运行时标记（RULE-021 / TX-001）",
        "schema": {"type": "object", "properties": {}},
        "handler": tool_get_tx_boundary,
    },
    "get_rules": {
        "description": "读取带来源与置信度的业务规则库",
        "schema": {"type": "object", "properties": {"min_confidence": {"type": "number"}}},
        "handler": tool_get_rules,
    },
    "get_plan": {
        "description": "读取迁移计划的风险清单与审批要求",
        "schema": {"type": "object", "properties": {}},
        "handler": tool_get_plan,
    },
    "get_gate_report": {
        "description": "读取行为等价性闸门报告，attempt=first 可读首次失败快照",
        "schema": {"type": "object", "properties": {"attempt": {"type": "string", "enum": ["first", "final"]}}},
        "handler": tool_get_gate_report,
    },
    "get_diff": {
        "description": "读取双跑差分结果（输出 / 异常 / SQL 轨迹）",
        "schema": {"type": "object", "properties": {"attempt": {"type": "string", "enum": ["first", "final"]}}},
        "handler": tool_get_diff,
    },
    "list_artifacts": {
        "description": "列出所有已落盘的中间产物及字节数",
        "schema": {"type": "object", "properties": {}},
        "handler": tool_list_artifacts,
    },
    "run_build": {
        "description": "真实执行 javac 四阶段编译（Java 8 存量 / JDK 17 目标）",
        "schema": {"type": "object", "properties": {}},
        "handler": tool_run_build,
    },
    "run_equiv_check": {
        "description": "真实执行行为等价性闸门：契约测试 + 双跑差分 + AST 语义巡检",
        "schema": {"type": "object", "properties": {}},
        "handler": tool_run_equiv_check,
    },
    "get_source": {
        "description": "读取存量或目标实现的真实 Java 源码",
        "schema": {"type": "object", "properties": {"which": {"type": "string", "enum": ["legacy", "modern"]}}},
        "handler": tool_get_source,
    },
}


def tools_list() -> dict:
    return {"tools": [
        {"name": name,
         "description": spec["description"],
         "inputSchema": spec["schema"]}
        for name, spec in TOOLS.items()
    ]}


def call_tool(name: str, arguments: dict) -> dict:
    spec = TOOLS.get(name)
    if not spec:
        return {"content": [{"type": "text", "text": _text({"error": "unknown tool: " + name})}],
                "isError": True}
    try:
        result = spec["handler"](arguments or {})
        return {"content": [{"type": "text", "text": _text(result)}], "isError": False}
    except Exception as error:  # 工具失败要可见，但不得打断协议流
        print("[mcp] tool error: " + traceback.format_exc(), file=sys.stderr, flush=True)
        return {"content": [{"type": "text", "text": _text({"error": str(error)})}],
                "isError": True}


def handle(message: dict):
    method = message.get("method")
    identifier = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": identifier, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": identifier, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": identifier, "result": tools_list()}
    if method == "tools/call":
        result = call_tool(params.get("name", ""), params.get("arguments", {}))
        return {"jsonrpc": "2.0", "id": identifier, "result": result}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": identifier, "result": {"resources": []}}
    if identifier is None:
        return None
    return {"jsonrpc": "2.0", "id": identifier,
            "error": {"code": -32601, "message": "method not found: " + str(method)}}


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print("[mcp] server ready · {} tools".format(len(TOOLS)), file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            print("[mcp] invalid json: " + line[:120], file=sys.stderr, flush=True)
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
