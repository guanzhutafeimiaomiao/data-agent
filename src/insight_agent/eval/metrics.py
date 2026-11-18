"""执行准确率（EX）与汇总指标。

EX 的判定：两条 SQL 在同一个库上跑出来的结果集是否等价。
等价 = 行数相同 + 每行内容相同（数值按两位小数归一）+ 与行顺序无关，
并且允许列顺序不同（模型经常把"地区, 金额"写成"金额, 地区"）。
"""

from __future__ import annotations

import itertools
from typing import Any


def _norm_value(value: Any) -> Any:
    """把数值和"长得像数值的字符串"归一到同一形态。

    SQLite 的 strftime('%Y', ...) 返回文本，而 CAST(...) 返回整数，
    两者是同一个年份，不该判错——这是类型亲和性，不是数据错误。
    """
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        number = float(value)
        return str(int(number)) if number.is_integer() else str(round(number, 2))
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return text
        return str(int(number)) if number.is_integer() else str(round(number, 2))
    return value


def _norm_rows(rows: list[list[Any]]) -> list[tuple]:
    normalized = [tuple(_norm_value(v) for v in row) for row in rows]
    return sorted(normalized, key=lambda row: tuple(str(v) for v in row))


def _pivot_equivalent(gold: list[list[Any]], pred: list[list[Any]]) -> bool:
    """宽表 vs 长表：一行多值 与 多行(label, value) 视为等价。

    "2023 和 2024 的销售额分别是多少"这种问题，返回两条记录和返回一行两个数
    都是合理答案，不该因为形状不同判错。
    """
    if len(pred) != 1 or len(gold) < 2 or len(gold[0]) != 2:
        return False

    pred_values = [value for value in pred[0] if isinstance(value, (int, float))]
    gold_values = [row[1] for row in gold if isinstance(row[1], (int, float))]
    if not pred_values or len(pred_values) != len(gold_values):
        return False

    return sorted(round(float(v), 2) for v in pred_values) == sorted(
        round(float(v), 2) for v in gold_values
    )


def result_equal(gold: list[list[Any]], pred: list[list[Any]], permute: bool = True) -> bool:
    if len(gold) != len(pred):
        return _pivot_equivalent(gold, pred)
    if _norm_rows(gold) == _norm_rows(pred):
        return True
    if not permute or not gold or len(gold[0]) > 4:
        return False

    width = len(gold[0])
    # 列数对不上就别谈"列序不同"了，直接判错（之前这里会 IndexError）
    if not pred or any(len(row) != width for row in pred):
        return False

    for perm in itertools.permutations(range(width)):
        transformed = [tuple(row[i] for i in perm) for row in pred]
        if _norm_rows(gold) == _norm_rows(transformed):
            return True
    return False


def summarize(records: list[dict]) -> dict:
    total = len(records)
    if total == 0:
        return {"total": 0}

    ok = [r for r in records if r.get("ex")]
    errored = [r for r in records if r.get("error")]
    graded = [r for r in records if not r.get("error")]

    first_try = [r for r in graded if r.get("attempts") and r["attempts"][0].get("verdict") == "ok"]
    repaired = [r for r in ok if not (r.get("attempts") and r["attempts"][0].get("verdict") == "ok")]

    def avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 2) if values else 0.0

    by_tag: dict[str, dict] = {}
    for record in graded:
        tag = record.get("difficulty", "unknown")
        bucket = by_tag.setdefault(tag, {"total": 0, "ok": 0})
        bucket["total"] += 1
        bucket["ok"] += 1 if record.get("ex") else 0
    for bucket in by_tag.values():
        bucket["ex_rate"] = round(bucket["ok"] / bucket["total"], 3)

    return {
        "total": total,
        "graded": len(graded),
        "errored": len(errored),
        "ex_rate": round(len(ok) / len(graded), 3) if graded else 0.0,
        "first_try_rate": round(len(first_try) / len(graded), 3) if graded else 0.0,
        "repaired_ok": len(repaired),
        "avg_attempts": avg([len(r.get("attempts", [])) for r in graded]),
        "avg_tokens": avg([r.get("tokens", 0) for r in graded]),
        "avg_seconds": avg([r.get("seconds", 0.0) for r in graded]),
        "by_difficulty": by_tag,
    }
