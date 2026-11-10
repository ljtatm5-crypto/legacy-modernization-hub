"""测试用例生成：边界值分析 + 等价类划分 + 两两组合（IPOG）+ 失败用例最小化（ddmin）。

为什么需要它：
手工挑的 5 个用例覆盖的是「想得到的场景」，而缺陷通常长在「没想到的边界」上。
本模块把输入空间按等价类和边界值切开，用两两组合把参数交叉覆盖压到可接受规模，
再用 ddmin 把失败用例收缩成最小复现，方便定位与回灌给生成器。

只依赖标准库。
"""
from __future__ import annotations

import itertools
import random

# --------------------------------------------------------------------------
# 1. 等价类与边界值
# --------------------------------------------------------------------------
AMOUNT_CLASSES = [
    ("非法·零", 0.00),
    ("非法·负数", -50.00),
    ("零邻域·下界内", 0.001),      # 大于 0 但四舍五入到 0.00：最容易漏的区间
    ("零邻域·上界前", 0.004),
    ("零邻域·进位点", 0.005),
    ("常规·整额", 12800.00),
    ("常规·三位小数", 99.995),
    ("舍入边界·半分进位", 100.005),
    ("舍入边界·半分不进位", 100.015),
    ("大额·十亿", 1000000000.00),
]

ORDER_ID_SHAPES = [
    ("标准前缀", "SO-2026-{seq}"),
    ("小写前缀", "so-2026-{seq}"),
    ("带扩展段", "SO-2026-{seq}-EXT"),
]

QUANTITIES = [1, 2, 3]


def _expand(dimensions: list) -> list:
    """全组合，orderId 保留模板，序化留到 generate() 里做。"""
    names = [name for name, _ in dimensions]
    return [dict(zip(names, combo))
            for combo in itertools.product(*[values for _, values in dimensions])]


# --------------------------------------------------------------------------
# 2. 两两组合（IPOG，t=2）
# --------------------------------------------------------------------------
def _pairs(row: dict, names: list) -> set:
    return {(names[i], row[names[i]], names[j], row[names[j]])
            for i in range(len(names)) for j in range(i + 1, len(names))}


def _covers(row: dict, names: list, pair: tuple) -> bool:
    a, av, b, bv = pair
    return row.get(a) == av and row.get(b) == bv


def pairwise(dimensions: list) -> list:
    """两两组合：先取全组合，再用贪心集合覆盖删冗余行。

    删除条件是「该行覆盖的每一对取值，在剩余行里仍至少出现一次」，
    因此两两覆盖率恒为 1，不会因为贪心顺序而漏对。
    全组合在这里最多 90 行，代价可忽略；真实参数更多时才需要 IPOG 的水平扩展。
    """
    names = [name for name, _ in dimensions]
    rows = _expand(dimensions)
    counts = {}
    for row in rows:
        for pair in _pairs(row, names):
            counts[pair] = counts.get(pair, 0) + 1

    kept = []
    # 固定顺序保证可复现，不用随机打乱
    for row in sorted(rows, key=lambda item: [str(item[name]) for name in names]):
        pairs = _pairs(row, names)
        if pairs and all(counts[pair] > 1 for pair in pairs):
            for pair in pairs:
                counts[pair] -= 1
        else:
            kept.append(row)
    return kept


def generate(use_pairwise: bool = True) -> dict:
    dimensions = [
        ("amount", [value for _, value in AMOUNT_CLASSES]),
        ("quantity", QUANTITIES),
        ("orderId", [shape for _, shape in ORDER_ID_SHAPES]),
    ]
    if use_pairwise:
        rows = pairwise(dimensions)
        strategy = "pairwise(贪心集合覆盖, t=2)"
    else:
        rows = _expand(dimensions)
        strategy = "full-cartesian"
    # 覆盖率必须在 orderId 序化之前算，否则模板值对不上
    coverage = pairwise_coverage(rows, dimensions)
    total = 1
    for _, vals in dimensions:
        total *= len(vals)
    seq = 880
    for row in rows:
        seq += 1
        row["orderId"] = row["orderId"].format(seq=seq)
        row["case"] = row["orderId"]
    return {
        "strategy": strategy,
        "dimensions": {name: len(vals) for name, vals in dimensions},
        "full_space": total,
        "case_count": len(rows),
        "coverage_t2": round(coverage, 3),
        "reduction": round(1 - len(rows) / total, 3) if total else 0,
        "classes": [{"class": name, "value": value} for name, value in AMOUNT_CLASSES],
        "cases": rows,
    }


def pairwise_coverage(rows: list, dimensions: list) -> float:
    """校验两两覆盖率，必须为 1.0，否则生成器有 bug。"""
    names = [name for name, _ in dimensions]
    needed = set()
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            for a in dict(dimensions)[names[i]]:
                for b in dict(dimensions)[names[j]]:
                    needed.add((names[i], a, names[j], b))
    got = set()
    for row in rows:
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                got.add((names[i], row.get(names[i]), names[j], row.get(names[j])))
    return len(needed & got) / len(needed) if needed else 1.0


# --------------------------------------------------------------------------
# 3. ddmin：把失败输入收缩成最小复现
# --------------------------------------------------------------------------
def ddmin(amount: float, fails, low: float = 0.0, high: float = None) -> float:
    """在 [low, high) 区间上二分查找仍能触发失败的最小金额。

    fails(value) -> bool 由调用方提供（真实跑差分），不是猜测。
    """
    high = amount if high is None else high
    best = amount
    for _ in range(40):
        if high - low <= 1e-6:
            break
        mid = round((low + high) / 2, 6)
        if fails(mid):
            best = mid
            high = mid
        else:
            low = mid
    return best


def write_cases_txt(rows: list, path) -> int:
    """写成 Harness 可直接读的纯文本，避免在 Java 侧引入 JSON 解析。"""
    lines = ["# orderId|amount|quantity  由 testgen 生成，勿手改"]
    for row in rows:
        lines.append("{}|{}|{}".format(row["case"], row["amount"], row["quantity"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    report = generate(True)
    print(json.dumps({k: report[k] for k in
                      ("strategy", "dimensions", "full_space", "case_count",
                       "coverage_t2", "reduction")}, ensure_ascii=False))
    if len(sys.argv) > 1:
        n = write_cases_txt(report["cases"], Path(sys.argv[1]))
        print("wrote", n, "cases")
