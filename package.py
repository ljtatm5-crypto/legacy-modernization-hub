"""打包脚本：产出一个可以直接发给别人、双击就能跑的 zip。

用法：
  python package.py                  # 不含密钥，接收方需自己配 model.json
  python package.py --with-key       # 把本机 hub/model.json 一起打进去（仅限本机演示）
  python package.py --with-artifacts # 连当前产物一起打包，接收方打开即可看到数据

默认排除：artifacts / runs / build / __pycache__ / .git / 密钥。
"""
from __future__ import annotations

import argparse
import os
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

INCLUDE = [
    "legacy-modernization-hub.html",
    "hub/workflow.yaml",
    "hub/model.example.json",
    "start.bat", "stop.bat", "start.sh", "stop.sh",
    "README.md", ".gitignore", "docs", "screenshots",
    "hub/backend",
    "hub/engine",
    "hub/mcp-server",
    "hub/templates",
    "hub/sample-project",
    # 工具自身的实验证据：基线臂、A/B 配对、基准、实验协议、真实数据集扫描
    "hub/artifacts/w1",
    "hub/artifacts/dataset",
]
OPTIONAL = ["hub/artifacts", "hub/runs", "hub/model.json"]

DATASET_SRC = ROOT / "_dataset" / "repo"
DATASET_ARC = "dataset/commons-lang"
DATASET_LICENSE = ["LICENSE.txt", "NOTICE.txt", "README.md", "pom.xml"]


def add_dataset(zip_file: zipfile.ZipFile, counter: list) -> None:
    """把真实数据集（源码 + 许可证）打进包，去掉 .git 以控制体积。"""
    java_root = DATASET_SRC / "src" / "main" / "java"
    if not java_root.exists():
        print("  ! 未找到 _dataset/repo/src/main/java，跳过数据集")
        return
    java_count = 0
    for file in sorted(java_root.rglob("*.java")):
        relative = file.relative_to(java_root).as_posix()
        zip_file.write(file, "{}/src/main/java/{}".format(DATASET_ARC, relative))
        java_count += 1
    count = java_count
    for name in DATASET_LICENSE:
        path = DATASET_SRC / name
        if path.exists():
            zip_file.write(path, "{}/{}".format(DATASET_ARC, name))
            count += 1

    commit = remote = "unknown"
    try:
        import subprocess
        commit = subprocess.run(["git", "-C", str(DATASET_SRC), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip() or "unknown"
        remote = subprocess.run(["git", "-C", str(DATASET_SRC), "config", "--get", "remote.origin.url"],
                                capture_output=True, text=True).stdout.strip() or "unknown"
    except Exception:
        pass

    note = "\n".join([
        "数据集：Apache Commons Lang（src/main/java）",
        "=" * 52,
        "来源仓库：" + remote,
        "提交版本：" + commit,
        "抓取时间：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "文件数量：{} 个 .java".format(java_count),
        "",
        "用途",
        "  真实代码库迁移风险巡检的输入。引擎里的 SemanticScan 面向本样例模块，",
        "  LegacyScan 是通用巡检器，可以直接跑这个目录。",
        "",
        "复现",
        "  python hub/engine/cli.py dataset --preset commons-lang",
        "  预期：264 个文件 / 397 个类 / 4579 个方法 / 42 条风险",
        "        （java.util.Date 20、空 catch 17、SimpleDateFormat 5）",
        "",
        "许可证",
        "  Apache License 2.0，见同目录 LICENSE.txt 与 NOTICE.txt。",
        "  本包只分发源码与许可证，不修改、不重新打包其构建产物。",
        "  未包含 .git 目录，提交版本以上方 commit 为准。",
    ]) + "\n"
    zip_file.writestr(DATASET_ARC + "/SOURCE.txt", note)
    counter[0] += count + len(DATASET_LICENSE) + 1
    print("  + 已包含真实数据集：{} 个 java 文件 + {} 个许可证/说明文件".format(
        java_count, len(DATASET_LICENSE) + 1))

SKIP_DIRS = {"__pycache__", ".git", "build", "dist", "_dataset", "node_modules", ".idea", ".vscode"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}


def iter_files(base: Path):
    for path in sorted(base.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(base).parts[:-1]):
            continue
        if path.is_dir() or path.name in SKIP_FILES:
            continue
        if path.suffix in {".pyc"}:
            continue
        yield path


def add(zip_file: zipfile.ZipFile, source: Path, arc_prefix: str, counter: list) -> None:
    for file in iter_files(source):
        relative = file.relative_to(source)
        zip_file.write(file, str(Path(arc_prefix) / relative))
        counter[0] += 1


def _human(size: int) -> str:
    return "{:,} B ({:.1f} KB)".format(size, size / 1024)


def build_manifest(zip_file: zipfile.ZipFile, args) -> str:
    infos = [i for i in zip_file.infolist() if not i.filename.startswith("MANIFEST")]
    raw = sum(i.file_size for i in infos)
    comp = sum(i.compress_size for i in infos)

    buckets = {}
    for info in infos:
        parts = info.filename.split("/")
        key = "/".join(parts[:2]) if len(parts) > 1 else "(根目录)"
        count, size = buckets.get(key, (0, 0))
        buckets[key] = (count + 1, size + info.file_size)

    lines = [
        "Legacy Modernization Hub · 分发包清单",
        "=" * 52,
        "生成时间：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "文件数：" + str(len(infos)) + "（不含本清单）",
        "解压后：" + _human(raw),
        "压缩后：" + _human(comp) + "   压缩率 {:.1f}%".format(comp / raw * 100 if raw else 0),
        "",
        "为什么只有这么大",
        "-" * 52,
        "1. 零第三方依赖：Node 用原生 http，Python 只用标准库，不需要 npm install / pip install。",
        "   如果带 node_modules，体积会从 0.1 MB 变成几十上百 MB。",
        "2. 没有二进制资源：界面是纯 CSS + SVG，没有图片、字体、视频。",
        "3. 编译产物不入包：Java class 在首次运行时自动编译（hublib.ensure_ast_classes）。",
        "4. 运行产物不入包：artifacts 由运行生成，每次都能重跑出来。",
        "",
        "按目录构成（解压后）",
        "-" * 52,
    ]
    for key, (count, size) in sorted(buckets.items(), key=lambda kv: -kv[1][1]):
        lines.append("  {:<34s} {:>3} 个文件  {:>10,} B".format(key, count, size))

    has_dataset = any(i.filename.startswith("dataset/") for i in infos)
    lines += [
        "",
        "随包的真实数据集" if has_dataset else "刻意不含的东西",
        "-" * 52,
    ]
    if has_dataset:
        ds = [i for i in infos if i.filename.startswith("dataset/")]
        lines += [
            "  dataset/commons-lang/  {} 个文件  {:,} B".format(
                len(ds), sum(i.file_size for i in ds)),
            "  Apache Commons Lang（Apache-2.0），含 LICENSE.txt / NOTICE.txt / SOURCE.txt。",
            "  复现：python hub/engine/cli.py dataset --preset commons-lang",
            "  预期：264 个文件 / 397 个类 / 4579 个方法 / 42 条风险。",
            "  未包含 .git（省约 1 MB），提交版本见 SOURCE.txt。",
        ]
    else:
        lines += ["  未包含真实数据集（瘦包）。需要时用完整包，或自行克隆后 --path 指定。"]

    lines += [
        "",
        "刻意不含的东西",
        "-" * 52,
        "  hub/artifacts/**（除 w1 与 dataset 证据）  运行产物，首次运行自动生成",
        "  hub/engine/ast_scan/build/**             巡检器 class，首次运行自动编译",
        "  hub/model.json" + ("" if args.with_key else "                            密钥（未随包分发）"),
        "  hub/runs/**                              历史轮次归档",
        "",
        "启动",
        "-" * 52,
        "  Windows       双击 start.bat",
        "  macOS / Linux ./start.sh",
        "  停止          stop.bat / ./stop.sh",
        "  环境要求      Node 18+ / JDK 17+ / Python 3.9+",
        "",
        "自检",
        "-" * 52,
        "  python hub/engine/cli.py probe        # 生成适配器是否可用",
        "  python hub/engine/cli.py run --restart  # 跑一次完整链路",
        "  python hub/mcp-server/smoke_test.py   # MCP 服务自检",
    ]
    return "\n".join(lines) + "\n"


GITHUB_INCLUDE = [
    "README.md", "LICENSE", "GITHUB.md", ".gitignore", ".gitattributes",
    "start.bat", "stop.bat", "start.sh", "stop.sh", "package.py",
    "legacy-modernization-hub.html",
    "docs", "screenshots", ".github",
    "hub/workflow.yaml", "hub/model.example.json",
    "hub/backend", "hub/engine", "hub/mcp-server", "hub/templates", "hub/sample-project",
]
GITHUB_ROOT = "legacy-modernization-hub"
# 目前没有需要剔除的文件：生成的目标实现要留着，否则干净仓库下 build 会失败
GITHUB_SKIP = set()
# 统一成 LF 提交，避免 Windows 上 git add 时刷一屏 CRLF 警告
CRLF_KEEP = {".bat"}
TEXT_SUFFIX = {".py", ".js", ".md", ".html", ".java", ".json", ".yaml", ".yml",
               ".tpl", ".sh", ".txt", ".gitignore", ".gitattributes", ""}


def _write_normalized(zip_file, file, arcname):
    """文本文件按 LF 写入（.bat 保留 CRLF）。"""
    if file.suffix.lower() in CRLF_KEEP:
        zip_file.writestr(arcname, file.read_bytes())
        return
    if file.suffix.lower() in TEXT_SUFFIX or file.name in (".gitignore", ".gitattributes"):
        text = file.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        zip_file.writestr(arcname, text.encode("utf-8"))
        return
    zip_file.write(file, arcname)


def build_github(target: Path) -> int:
    """仓库结构包：一个顶层目录，解压后就是仓库根。"""
    counter = [0]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zip_file:
        for item in GITHUB_INCLUDE:
            path = ROOT / item
            if not path.exists():
                print("  ! 跳过（不存在）：{}".format(item))
                continue
            if path.is_dir():
                for file in iter_files(path):
                    if file.name in GITHUB_SKIP:
                        continue
                    relative = file.relative_to(path)
                    _write_normalized(zip_file, file, str(Path(GITHUB_ROOT) / item / relative))
                    counter[0] += 1
            else:
                _write_normalized(zip_file, path, str(Path(GITHUB_ROOT) / item))
                counter[0] += 1

    with zipfile.ZipFile(target) as check:
        names = check.namelist()
        unpacked = sum(i.file_size for i in check.infolist())
    banned = [n for n in names if "/artifacts/" in n or n.endswith("model.json")
              or "/_dataset/" in n or "/runs/" in n or n.endswith(".pyc")]
    if banned:
        print("  ! 包内有不该提交的东西：{}".format(banned[:5]))
        return 1

    print("")
    print("  仓库包：{}".format(target))
    print("  文件数：{}  体积：{:.1f} MB  解压后：{:.1f} MB".format(
        len(names), target.stat().st_size / 1024 / 1024, unpacked / 1024 / 1024))
    print("  顶层目录：{}（解压后即为仓库根）".format(GITHUB_ROOT))
    print("  已核对：不含 artifacts / model.json / _dataset / runs")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Legacy Modernization Hub")
    parser.add_argument("--with-key", action="store_true", help="把本机 model.json 打进去")
    parser.add_argument("--with-artifacts", action="store_true", help="连当前产物一起打包")
    parser.add_argument("--slim", action="store_true", help="瘦包：不带真实数据集（约 130 KB）")
    parser.add_argument("--github", action="store_true",
                        help="产出可直接 git push 的仓库结构包（不含产物/密钥/数据集克隆）")
    parser.add_argument("--out", default="", help="输出文件名")
    args = parser.parse_args()

    if args.github:
        stamp = datetime.now().strftime("%Y%m%d")
        name = args.out or "legacy-modernization-hub-github-{}.zip".format(stamp)
        return build_github(DIST / name)

    stamp = datetime.now().strftime("%Y%m%d")
    name = args.out or "legacy-modernization-hub-{}.zip".format(stamp)
    DIST.mkdir(exist_ok=True)
    target = DIST / name

    # 打包前确保巡检器已编译（接收方没有 JDK 编译器时也能跑）
    engine = str(ROOT / "hub" / "engine")
    os.system('cd "{}" && python -c "import sys; sys.path.insert(0,\'.\'); '
              'from hublib import ensure_ast_classes; ensure_ast_classes()" >nul 2>nul'.format(engine))

    counter = [0]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zip_file:
        for item in INCLUDE:
            if args.slim and item in ("docs", "screenshots"):
                continue  # 瘦包只要代码
            path = ROOT / item
            if not path.exists():
                print("  ! 跳过（不存在）：{}".format(item))
                continue
            if path.is_dir():
                add(zip_file, path, item, counter)
            else:
                zip_file.write(path, item)
                counter[0] += 1

        if args.with_artifacts and (ROOT / "hub" / "artifacts").exists():
            add(zip_file, ROOT / "hub" / "artifacts", "hub/artifacts", counter)
            print("  + 已包含当前产物（打开即见数据）")

        if args.with_key:
            model = ROOT / "hub" / "model.json"
            if model.exists():
                zip_file.write(model, "hub/model.json")
                counter[0] += 1
                print("  ! 已包含密钥 hub/model.json —— 只在本机演示时使用，不要外发")
            else:
                print("  ! 本机没有 hub/model.json，未包含密钥")
        else:
            print("  · 不含密钥：接收方需复制 hub/model.example.json → hub/model.json 并填 key")

        if not args.slim:
            add_dataset(zip_file, counter)

        zip_file.writestr("MANIFEST.txt", build_manifest(zip_file, args))

    size = target.stat().st_size / 1024 / 1024
    # 以 zip 实际条目为准，避免手工计数器重复累加
    with zipfile.ZipFile(target) as check:
        real = len([n for n in check.namelist() if n != "MANIFEST.txt"])
        unpacked = sum(i.file_size for i in check.infolist())
    print("")
    print("  打包完成：{}".format(target))
    print("  文件数：{}  体积：{:.1f} MB".format(real, size))
    print("  解压后：{:.1f} MB".format(unpacked / 1024 / 1024))
    print("")
    print("  接收方操作：解压 → 双击 start.bat（mac/linux: ./start.sh）→ 浏览器自动打开")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
