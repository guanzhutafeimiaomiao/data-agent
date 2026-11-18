"""演示用的电商库。确定性生成，方便评测复现，也避免一上来就依赖外部数据集。"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

REGIONS = ["华东", "华北", "华南", "西南"]

DDL = """
CREATE TABLE customers (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    region  TEXT NOT NULL,
    cohort  TEXT NOT NULL
);
CREATE TABLE products (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    list_price REAL NOT NULL
);
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date  TEXT NOT NULL,
    channel     TEXT NOT NULL,
    status      TEXT NOT NULL
);
CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity   INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    discount   REAL NOT NULL
);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_items_order ON order_items(order_id);
"""

CATEGORIES = {
    "数码": ["耳机", "键盘", "显示器", "充电宝", "摄像头"],
    "家居": ["台灯", "收纳箱", "抱枕", "香薰机", "地毯"],
    "服饰": ["T恤", "外套", "牛仔裤", "运动鞋", "背包"],
    "食品": ["坚果礼盒", "咖啡豆", "茶叶", "蜂蜜", "巧克力"],
}


def build_demo_db(path: str | Path, seed: int = 42) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return path

    rng = random.Random(seed)
    con = sqlite3.connect(path)
    try:
        con.executescript(DDL)
        con.executemany(
            "INSERT INTO customers VALUES (?,?,?,?)",
            [
                (i, f"客户{i:03d}", rng.choice(REGIONS), rng.choice(["2022", "2023", "2024"]))
                for i in range(1, 201)
            ],
        )

        products = []
        pid = 1
        for category, names in CATEGORIES.items():
            for name in names:
                for variant in ("标准款", "升级款"):
                    products.append((pid, f"{name}-{variant}", category, round(rng.uniform(39, 2999), 2)))
                    pid += 1
        con.executemany("INSERT INTO products VALUES (?,?,?,?)", products)

        orders, items = [], []
        item_id = 1
        for oid in range(1, 4001):
            day = rng.randint(0, 1095)  # 2022-01-01 ~ 2024-12-31
            date = f"{2022 + day // 365}-{day % 365 // 31 + 1:02d}-{day % 31 + 1:02d}"
            status = rng.choices(["paid", "refunded", "pending"], weights=[85, 10, 5])[0]
            orders.append((oid, rng.randint(1, 200), date, rng.choice(["app", "web", "mini"]), status))
            for _ in range(rng.randint(1, 3)):
                product = rng.choice(products)
                items.append(
                    (
                        item_id,
                        oid,
                        product[0],
                        rng.randint(1, 4),
                        product[3],
                        rng.choice([0.0, 0.0, 0.05, 0.1, 0.15]),
                    )
                )
                item_id += 1

        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
        con.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", items)
        con.commit()
    finally:
        con.close()
    return path


def table_schema(con: sqlite3.Connection) -> list[dict]:
    """把库结构读成节点直接可用的形态。大库要换成向量检索，这里先全量。"""
    tables = [
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    schema = []
    for table in tables:
        columns = [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]
        schema.append({"table": table, "columns": columns})
    return schema
