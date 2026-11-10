"""共享工具：路径、日志、产物落盘、子进程执行。零第三方依赖。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Windows 控制台默认使用 GBK，日志中的状态符号会让 CLI 直接中断。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "sample-project"
TEMPLATES = ROOT / "templates"
ARTIFACTS = ROOT / "artifacts"
BUILD = PROJECT / "build"
ENGINE = ROOT / "engine"
AST_BUILD = ENGINE / "ast_scan" / "build"
COMMON_SRC = PROJECT / "common" / "src" / "com" / "demo" / "settle"
LEGACY_SRC = PROJECT / "legacy" / "src" / "com" / "demo" / "settle"
MODERN_SRC = PROJECT / "modern" / "src" / "com" / "demo" / "settle"
TESTS_SRC = PROJECT / "tests"
STATE_FILE = ARTIFACTS / "state.json"
CONFIG_FILE = ARTIFACTS / "config.json"
LOG_FILE = ARTIFACTS / "run.log"
WORKFLOW_FILE = ROOT / "workflow.yaml"
# 随包分发的真实数据集（开发态在 _dataset/repo，打包后在 dataset/commons-lang）
DATASET_CANDIDATES = [
    ROOT.parent / "dataset" / "commons-lang" / "src" / "main" / "java",
    ROOT.parent / "_dataset" / "repo" / "src" / "main" / "java",
]


def dataset_path(preset: str = "commons-lang") -> Path:
    """按预设名解析数据集路径，找不到就明确报错而不是静默返回空。"""
    for candidate in DATASET_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "未找到数据集「{}」，可尝试以下路径：\n  {}".format(
            preset, "\n  ".join(str(candidate) for candidate in DATASET_CANDIDATES)))

MODERN_CLASS = "com.demo.settle.ModernSettleService"
LEGACY_CLASS = "com.demo.settle.LegacySettleService"



def ensure_dirs() -> None:
    for path in [
        ARTIFACTS, BUILD / "common", BUILD / "legacy", BUILD / "modern",
        BUILD / "test", ARTIFACTS / "graph", ARTIFACTS / "rules",
        ARTIFACTS / "plan", ARTIFACTS / "src", ARTIFACTS / "equiv",
        ARTIFACTS / "test", ARTIFACTS / "report", ARTIFACTS / "patch",
        ARTIFACTS / "workflow", ARTIFACTS / "w1", ARTIFACTS / "audit",
    ]:
        path.mkdir(parents=True, exist_ok=True)


def ensure_ast_classes() -> bool:
    """语义巡检器需要先编译成 class；换机器/新解压后必须能自举。"""
    marker = AST_BUILD / "SemanticScan.class"
    if marker.exists() and (AST_BUILD / "LegacyScan.class").exists():
        return True
    sources = sorted((ENGINE / "ast_scan").glob("*.java"))
    if not sources:
        return False
    AST_BUILD.mkdir(parents=True, exist_ok=True)
    result = run(["javac", "-encoding", "UTF-8", "-d", str(AST_BUILD)] + [str(p) for p in sources])
    if result.returncode != 0:
        log("ERR", "语义巡检器编译失败 · {}".format((result.stderr or "").strip()[:300]))
        return False
    return True


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def iso_ms() -> str:
    """毫秒精度时间戳：轨迹里用得到，秒级会把人工签核间隔抹成 0。"""
    return datetime.now().isoformat(timespec="milliseconds")


def log(level: str, message: str) -> None:
    line = "[{}] [{}] {}".format(now(), level, message)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, obj) -> None:
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default=None):
    if not Path(path).exists():
        return default
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def _decode(raw: bytes) -> str:
    """Windows 上 javac 输出 GBK，直接按 UTF-8 解会变成乱码证据。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run(cmd, cwd=None):
    args = [str(part) for part in cmd]
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        shell=False,
    )
    return subprocess.CompletedProcess(
        args, completed.returncode,
        _decode(completed.stdout), _decode(completed.stderr))


def classpath(*parts) -> str:
    return os.pathsep.join(str(part) for part in parts)


def default_state(spec=None, source=None) -> dict:
    steps, nodes = [], {}
    if spec:
        try:
            from workflow import layers_of, order_of
            order = order_of(spec)
            layer = layers_of(spec, order)
            by_id = {node["id"]: node for node in spec["nodes"]}
            for node_id in order:
                nodes[node_id] = {"status": "pending", "attempts": 0,
                                  "artifacts": [], "duration_ms": 0}
                steps.append({
                    "id": by_id[node_id]["skill"], "node_id": node_id,
                    "name": by_id[node_id]["title"], "layer": layer[node_id],
                    "depends_on": by_id[node_id]["depends_on"], "status": "pending",
                    "artifacts": [], "duration_ms": 0, "attempts": 0,
                    "regenerated": False, "note": "",
                })
        except Exception:  # 工作流定义异常不应阻止会话初始化
            steps, nodes = [], {}
    return {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "started_at": iso(),
        "steps": steps,
        "nodes": nodes,
        "trace": [],
        "workflow": {
            "name": (spec or {}).get("name", "legacy-modernization"),
            "node_count": len((spec or {}).get("nodes", [])),
            "concurrency": (spec or {}).get("concurrency", 1),
            "source": source or {"kind": "builtin"},
        },
        "plan_approved": False,
        "inject_defect": False,
        "fixed": False,
        "rework": 0,
        "gate": "idle",
        "gate_results": {},
        "checkpoint": "not-started",
        "finished": False,
        "skipped_steps": 0,
        "generator": "rule-based-template-v1",
    }


def save_state(state: dict) -> None:
    write_json(STATE_FILE, state)


def load_state() -> dict:
    data = read_json(STATE_FILE, None)
    if data is not None:
        return data
    # 首次运行前没有 state.json：这里要把真实工作流来源带上，
    # 否则 /api/status 与 cli.py graph 会误报"内置 DAG"。
    try:
        import workflow
        spec, source = workflow.load()
        return default_state(spec, source)
    except Exception:
        return default_state()


def load_config() -> dict:
    config = read_json(CONFIG_FILE, {}) or {}
    config.setdefault("inject_defect", False)
    config.setdefault("model_adapter", "rule-based-template-v1")
    if "defect_mode" not in config:
        # 兼容旧的布尔开关
        config["defect_mode"] = "structural" if config["inject_defect"] else "off"
    return config
