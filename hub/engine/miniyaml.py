"""零依赖 YAML 子集解析器。

只覆盖工作流定义需要的语法：缩进映射、`- ` 序列、内联 `[a, b]` / `{k: v}`、
引号字符串、布尔 / 数字 / null、`|` 块文本、`#` 注释。
不支持锚点、多文档、复杂键。遇到不认识的写法直接报错并给出行号，
避免"静默解析成 None"这类排查成本极高的问题。
"""
from __future__ import annotations


class YamlError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            break
        out.append(char)
    return "".join(out).rstrip()


def _prepare(text: str) -> list:
    rows = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError("第 {} 行：缩进请使用空格".format(lineno))
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        rows.append((indent, stripped.strip(), lineno))
    return rows


def _split_key(content: str, lineno: int):
    quote, depth = None, 0
    for index, char in enumerate(content):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == ":" and depth == 0:
            if index + 1 == len(content) or content[index + 1] == " ":
                key = content[:index].strip()
                if not key:
                    raise YamlError("第 {} 行：键为空".format(lineno))
                return key.strip("\"'"), content[index + 1:].strip()
    raise YamlError("第 {} 行：缺少 `key: value` 结构 → {!r}".format(lineno, content))


def _split_flow(body: str) -> list:
    parts, depth, quote, buffer = [], 0, None, []
    for char in body:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            buffer.append(char)
        elif char in "[{":
            depth += 1
            buffer.append(char)
        elif char in "]}":
            depth -= 1
            buffer.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return parts


def _scalar(text: str, lineno: int):
    text = text.strip()
    if text == "":
        return None
    if text[0] == '"' and text.endswith('"') and len(text) > 1:
        return text[1:-1].replace('\\"', '"').replace("\\n", "\n")
    if text[0] == "'" and text.endswith("'") and len(text) > 1:
        return text[1:-1].replace("''", "'")
    if text.startswith("[") and text.endswith("]"):
        body = text[1:-1].strip()
        return [_scalar(part, lineno) for part in _split_flow(body)] if body else []
    if text.startswith("{") and text.endswith("}"):
        body = text[1:-1].strip()
        result = {}
        if body:
            for part in _split_flow(body):
                key, value = _split_key(part, lineno)
                result[key] = _scalar(value, lineno)
        return result
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_map(rows, index, indent):
    result = {}
    while index < len(rows):
        current_indent, content, lineno = rows[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise YamlError("第 {} 行：缩进突然加深，缺少上级键".format(lineno))
        if content.startswith("- "):
            break
        key, rest = _split_key(content, lineno)
        index += 1
        if rest in ("|", "|-", ">", ">-"):
            block, base = [], None
            while index < len(rows) and rows[index][0] > indent:
                if base is None:
                    base = rows[index][0]
                block.append(" " * max(0, rows[index][0] - base) + rows[index][1])
                index += 1
            joiner = "\n" if rest.startswith("|") else " "
            result[key] = joiner.join(block)
        elif rest == "":
            if index < len(rows) and rows[index][0] > indent:
                result[key], index = _parse_block(rows, index, rows[index][0])
            else:
                result[key] = None
        else:
            result[key] = _scalar(rest, lineno)
    return result, index


def _parse_seq(rows, index, indent):
    items = []
    while index < len(rows):
        current_indent, content, lineno = rows[index]
        if current_indent != indent or not content.startswith("-"):
            break
        body = content[1:].strip()
        index += 1
        if body == "":
            if index < len(rows) and rows[index][0] > indent:
                value, index = _parse_block(rows, index, rows[index][0])
                items.append(value)
            else:
                items.append(None)
        elif body.startswith("[") or body.startswith("{") or body[0] in "\"'":
            items.append(_scalar(body, lineno))
        elif ":" in body and (body.find(":") + 1 == len(body) or body[body.find(":") + 1] == " "):
            inner = indent + 2
            rows[index - 1] = (inner, body, lineno)
            value, index = _parse_map(rows, index - 1, inner)
            items.append(value)
        else:
            items.append(_scalar(body, lineno))
    return items, index


def _parse_block(rows, index, indent):
    if rows[index][1].startswith("-"):
        return _parse_seq(rows, index, indent)
    return _parse_map(rows, index, indent)


def loads(text: str):
    rows = _prepare(text)
    if not rows:
        return {}
    value, index = _parse_block(rows, 0, rows[0][0])
    if index != len(rows):
        raise YamlError("第 {} 行：无法归入任何块，检查缩进".format(rows[index][2]))
    return value


def load(path):
    from pathlib import Path
    return loads(Path(path).read_text(encoding="utf-8"))
