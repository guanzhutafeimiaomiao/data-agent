"""从模板批量生成评测集。

为什么用模板生成而不是手写 100 条：手写必然出错，而出错的"标准答案"会让整个
评测失去意义。这里每条 gold SQL 都会先在库里跑一遍，跑不通的直接构建失败。

问题覆盖三层难度：单维过滤 → 分组聚合 → 排序 / 多期对比。
"""

from __future__ import annotations

import json
from pathlib import Path

BASE_FROM = """FROM order_items oi
JOIN orders o ON o.id = oi.order_id
JOIN customers c ON c.id = o.customer_id
LEFT JOIN products p ON p.id = oi.product_id"""

METRICS = {
    "gmv": "ROUND(SUM(oi.quantity * oi.unit_price * (1 - oi.discount)), 2)",
    "orders": "COUNT(DISTINCT o.id)",
    "units": "SUM(oi.quantity)",
    "customers": "COUNT(DISTINCT o.customer_id)",
    "aov": (
        "ROUND(SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) * 1.0 "
        "/ COUNT(DISTINCT o.id), 2)"
    ),
}

METRIC_CN = {
    "gmv": "销售额",
    "orders": "订单数",
    "units": "销量",
    "customers": "客户数",
    "aov": "客单价",
}

YEARS = [2022, 2023, 2024]
REGIONS = ["华东", "华北", "华南", "西南"]
CATEGORIES = ["数码", "家居", "服饰", "食品"]
CHANNELS = ["app", "web", "mini"]


def _where(year=None, region=None, category=None, channel=None, years=None) -> str:
    parts = ["o.status = 'paid'"]
    if year:
        parts.append(f"o.order_date >= '{year}-01-01' AND o.order_date < '{year + 1}-01-01'")
    if years:
        parts.append(
            "o.order_date >= '{0}-01-01' AND o.order_date < '{1}-01-01'".format(
                min(years), max(years) + 1
            )
        )
    if region:
        parts.append(f"c.region = '{region}'")
    if category:
        parts.append(f"p.category = '{category}'")
    if channel:
        parts.append(f"o.channel = '{channel}'")
    return " AND ".join(parts)


def _single(metric: str, year: int, region: str | None = None, category: str | None = None) -> str:
    return (
        f"SELECT {METRICS[metric]} AS {metric}\n{BASE_FROM}\n"
        f"WHERE {_where(year=year, region=region, category=category)}"
    )


def _by_dimension(metric: str, year: int, dim: str) -> str:
    col = {"region": "c.region", "category": "p.category", "channel": "o.channel"}[dim]
    return (
        f"SELECT {col} AS {dim}, {METRICS[metric]} AS {metric}\n{BASE_FROM}\n"
        f"WHERE {_where(year=year)}\nGROUP BY {col}\nORDER BY {col}"
    )


def _top(metric: str, year: int, dim: str) -> str:
    col = {"region": "c.region", "category": "p.category", "channel": "o.channel"}[dim]
    return (
        f"SELECT {col} AS {dim}, {METRICS[metric]} AS {metric}\n{BASE_FROM}\n"
        f"WHERE {_where(year=year)}\nGROUP BY {col}\nORDER BY {metric} DESC\nLIMIT 1"
    )


def _compare(metric: str, region: str, y1: int, y2: int) -> str:
    return (
        f"SELECT '{y1}' AS year, {METRICS[metric]} AS {metric}\n{BASE_FROM}\n"
        f"WHERE {_where(year=y1, region=region)}\n"
        f"UNION ALL\n"
        f"SELECT '{y2}' AS year, {METRICS[metric]} AS {metric}\n{BASE_FROM}\n"
        f"WHERE {_where(year=y2, region=region)}"
    )


def build_cases() -> list[dict]:
    cases: list[dict] = []

    def add(question: str, sql: str, difficulty: str, kind: str):
        cases.append(
            {
                "id": f"c{len(cases) + 1:03d}",
                "question": question,
                "sql": sql,
                "difficulty": difficulty,
                "kind": kind,
            }
        )

    # 单维过滤：3 年 × 4 地区 × 3 指标
    for year in YEARS:
        for region in REGIONS:
            for metric in ("gmv", "orders", "aov"):
                add(f"{year} 年{region}的{METRIC_CN[metric]}是多少？", _single(metric, year, region=region), "easy", "single_region")

    # 品类过滤：3 年 × 4 品类 × 2 指标
    for year in YEARS:
        for category in CATEGORIES:
            for metric in ("gmv", "units"):
                add(f"{year} 年{category}品类的{METRIC_CN[metric]}是多少？", _single(metric, year, category=category), "easy", "single_category")

    # 分组聚合：地区 / 品类 / 渠道
    for year in YEARS:
        for metric in ("gmv", "orders"):
            add(f"{year} 年各地区的{METRIC_CN[metric]}分别是多少？", _by_dimension(metric, year, "region"), "medium", "group_region")
            add(f"{year} 年各品类的{METRIC_CN[metric]}分别是多少？", _by_dimension(metric, year, "category"), "medium", "group_category")
            add(f"{year} 年各渠道的{METRIC_CN[metric]}分别是多少？", _by_dimension(metric, year, "channel"), "medium", "group_channel")

    # 排序取第一
    for year in YEARS:
        for metric in ("gmv", "orders"):
            add(f"{year} 年哪个品类的{METRIC_CN[metric]}最高？", _top(metric, year, "category"), "hard", "top_category")

    # 多期对比
    for region in REGIONS:
        for metric in ("gmv", "orders"):
            add(f"2023 年和 2024 年{region}的{METRIC_CN[metric]}分别是多少？", _compare(metric, region, 2023, 2024), "hard", "compare_years")

    # 客户数：2 年 × 4 地区
    for year in (2023, 2024):
        for region in REGIONS:
            add(f"{year} 年{region}的客户数是多少？", _single("customers", year, region=region), "easy", "single_region")

    return cases


def write_cases(path: Path, cases: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")
    return path


def load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
