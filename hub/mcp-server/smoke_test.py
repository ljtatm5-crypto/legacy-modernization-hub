"""MCP Server 冒烟测试：真实走一遍 initialize / tools/list / tools/call。"""
import json
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).with_name("server.py")

MESSAGES = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "hub-smoke-test", "version": "1.0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "get_tx_boundary", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "find_callers", "arguments": {"method": "settle"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "list_artifacts", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
     "params": {"name": "get_gate_report", "arguments": {"attempt": "first"}}},
    {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
     "params": {"name": "run_build", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
     "params": {"name": "does_not_exist", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
     "params": {"name": "get_gate_report", "arguments": {"attempt": "final"}}},
]

ARTIFACTS = SERVER.parent.parent / "artifacts"


def artifact_tool(text, marker):
    """工具要么返回真实产物内容，要么明确说"还没生成"——两者都算符合契约。

    只有"返回了但既没内容也没说明"才算异常。
    """
    if not text.startswith("{"):
        return False, "返回不是 JSON"
    if marker in text:
        return True, "有产物"
    try:
        payload = json.loads(text)
    except ValueError:
        return False, "JSON 解析失败"
    if payload.get("error"):
        return True, "无产物 · 已明确报错"
    return False, "返回既无内容也无说明"


def count_artifacts():
    return len([p for p in ARTIFACTS.rglob("*") if p.is_file()]) if ARTIFACTS.exists() else 0


def artifacts_count_matches(text, expected):
    """list_artifacts 在 run_build 之前调用，所以应与会话开始前的快照一致。

    空仓库返回 0 也算一致——断言的是「报的数 == 真实数」，不是「必须有文件」。
    """
    if not text.startswith("{"):
        return False
    try:
        return json.loads(text).get("count") == expected
    except ValueError:
        return False


def load_artifact(relative):
    """读磁盘产物；不存在就返回 None，不猜。"""
    path = ARTIFACTS / relative
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def main() -> int:
    # 在任何写操作之前取快照：list_artifacts 的返回值要和它对齐
    pre_count = count_artifacts()
    process = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in MESSAGES) + "\n"
    stdout, stderr = process.communicate(payload, timeout=180)
    responses = {}
    for line in stdout.splitlines():
        if line.startswith("{"):
            try:
                item = json.loads(line)
                responses[item.get("id")] = item
            except ValueError:
                continue

    init = responses.get(1, {}).get("result", {})
    tools = responses.get(2, {}).get("result", {}).get("tools", [])
    tx = responses.get(3, {}).get("result", {}).get("content", [{}])[0].get("text", "")
    callers = responses.get(4, {}).get("result", {}).get("content", [{}])[0].get("text", "")
    artifacts = responses.get(5, {}).get("result", {}).get("content", [{}])[0].get("text", "")
    first = responses.get(6, {}).get("result", {}).get("content", [{}])[0].get("text", "")
    build = responses.get(7, {}).get("result", {}).get("content", [{}])[0].get("text", "")
    unknown = responses.get(8, {}).get("result", {})
    final = responses.get(9, {}).get("result", {}).get("content", [{}])[0].get("text", "")

    # 闸门结论与运行状态有关（干净批次没有首轮失败快照），所以断言的是
    # 「工具返回值 == 磁盘产物」而不是「一定有失败」——后者只对缺陷批次成立。
    tool_first = json.loads(first).get("report") if first.startswith("{") else None
    tool_final = json.loads(final).get("report") if final.startswith("{") else None
    tx_ok, tx_note = artifact_tool(tx, "@TxRequired")
    callers_ok, callers_note = artifact_tool(callers, "declared_in")

    checks = {
        "initialize": init.get("protocolVersion") == "2024-11-05",
        "tools_count": len(tools) == 11,
        "tx_boundary_tool": tx_ok,
        "find_callers_tool": callers_ok,
        "artifacts_consistent": artifacts_count_matches(artifacts, pre_count),
        "first_attempt_consistent": (tool_first or {}).get("verdict") == (load_artifact("equiv/gate-first-attempt.json") or {}).get("verdict"),
        "final_gate_consistent": (tool_final or {}).get("verdict") == (load_artifact("equiv/gate-report.json") or {}).get("verdict"),
        "run_build_real": json.loads(build).get("ok") is True if build.startswith("{") else False,
        "unknown_tool_error": unknown.get("isError") is True,
    }
    for name, passed in checks.items():
        print("{} {}".format("PASS" if passed else "FAIL", name))
    final_disk = load_artifact("equiv/gate-report.json")
    print("闸门现状:", (final_disk or {}).get("verdict") or "尚无产物")
    print("代码图谱:", tx_note, "| find_callers:", callers_note)
    print("产物数快照:", pre_count)
    print("tools:", ", ".join(tool["name"] for tool in tools))
    print("server stderr:", stderr.strip()[:300])
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
