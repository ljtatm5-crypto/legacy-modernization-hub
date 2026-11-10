"""从真实源码抽取公共接口面，供生成适配层做提示词上下文。

模型最容易犯的错不是逻辑写错，而是**臆造接口**：把存量里返回 double 的
getter 当成返回 BigDecimal，或者调用不存在的方法重载。这类错误自检抓不到，
只能等编译器报错。所以把 common 包的公共签名真实抽出来喂给模型，
比在提示词里写十句"不要臆造接口"都管用。
"""
from __future__ import annotations

import re
from pathlib import Path

from hublib import COMMON_SRC

KEEP = ("public", "protected")


def _interesting(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith(("//", "*", "/*")):
        return False
    if stripped.startswith("@"):
        return True
    return stripped.startswith(KEEP) and "(" in stripped or stripped.startswith("public class") \
        or stripped.startswith("public interface") or stripped.startswith("public @")


def signatures(sources=None) -> str:
    """返回可直接放进提示词的接口面文本。"""
    files = sorted((sources or COMMON_SRC).glob("*.java"))
    lines = []
    for path in files:
        body = path.read_text(encoding="utf-8").splitlines()
        block = []
        for line in body:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(("package", "import")):
                continue
            if stripped.startswith(("@", "public", "protected")):
                block.append(stripped)
        if block:
            lines.append("// " + path.name)
            lines.extend(block)
            lines.append("")
    return "\n".join(lines).strip()


def callable_names(sources=None) -> set:
    text = signatures(sources)
    return set(re.findall(r"\b(\w+)\s*\(", text))
