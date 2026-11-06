# -*- coding: utf-8 -*-
"""商品与记忆库存储层。

默认 SQLite（零依赖）；在 data/db_config.json 里把 backend 改成 mysql 并填连接串，
即可切换到 MySQL，不需要改业务代码。MySQL 连接失败时自动回落 SQLite 并在 /api/config 里提示。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
# 只读文件系统（如 Vercel）下把可写数据放到 APP_WRITE_DIR（通常是 /tmp）
WRITE_DIR = os.environ.get("APP_WRITE_DIR") or DATA_DIR
CONFIG_PATH = os.path.join(DATA_DIR, "db_config.json")
SQLITE_PATH = os.path.join(WRITE_DIR, "shop.db")
CATALOG_PATH = os.path.join(DATA_DIR, "catalog.json")
LEGACY_MEMORY = os.path.join(DATA_DIR, "memory.json")

_lock = threading.RLock()
_STATE = {"backend": None, "warning": ""}

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT,
    brand TEXT,
    category TEXT,
    brand_tier TEXT,
    price REAL,
    origin TEXT,
    payload TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_origin ON products(origin);
CREATE TABLE IF NOT EXISTS memory (
    user_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at TEXT
);
"""

MYSQL_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS products (
        id VARCHAR(64) PRIMARY KEY,
        name VARCHAR(160),
        brand VARCHAR(80),
        category VARCHAR(64),
        brand_tier VARCHAR(32),
        price DECIMAL(10,2),
        origin VARCHAR(32),
        payload LONGTEXT NOT NULL,
        updated_at DATETIME,
        INDEX idx_products_category (category),
        INDEX idx_products_origin (origin)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS memory (
        user_id VARCHAR(128) PRIMARY KEY,
        payload LONGTEXT NOT NULL,
        updated_at DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


def _conf():
    conf = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except (json.JSONDecodeError, OSError):
            conf = {}
    backend = (os.environ.get("DB_BACKEND") or conf.get("backend") or "sqlite").lower()
    mysql = conf.get("mysql", {}) or {}
    mysql = {
        "host": os.environ.get("MYSQL_HOST") or mysql.get("host", "127.0.0.1"),
        "port": int(os.environ.get("MYSQL_PORT") or mysql.get("port", 3306)),
        "user": os.environ.get("MYSQL_USER") or mysql.get("user", "root"),
        "password": os.environ.get("MYSQL_PASSWORD") or mysql.get("password", ""),
        "database": os.environ.get("MYSQL_DATABASE") or mysql.get("database", "smart_shopping"),
    }
    return backend, mysql


def connect():
    """返回 (driver, connection, actual_backend)。MySQL 不可用时自动回落。"""
    backend, mysql = _conf()
    if backend == "mysql":
        try:
            import pymysql
        except ImportError:
            _STATE["warning"] = "未安装 pymysql，已回落 SQLite（pip install pymysql 后可用 MySQL）"
            backend = "sqlite"
        else:
            try:
                conn = pymysql.connect(host=mysql["host"], port=mysql["port"], user=mysql["user"],
                                       password=mysql["password"], database=mysql["database"],
                                       charset="utf8mb4", autocommit=True, connect_timeout=6)
                _STATE.update(backend="mysql", warning="")
                return "mysql", conn, "mysql"
            except Exception as exc:
                _STATE["warning"] = f"MySQL 连接失败，已回落 SQLite（{type(exc).__name__}）"
                backend = "sqlite"
    os.makedirs(WRITE_DIR, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH, timeout=10, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:  # Wasm/只读磁盘等环境不支持 WAL 时退回默认日志模式
        pass
    _STATE["backend"] = "sqlite"
    return "sqlite", conn, "sqlite"


def _ensure_schema(driver, conn):
    if driver == "sqlite":
        conn.executescript(SQLITE_SCHEMA)
        conn.commit()
    else:
        with conn.cursor() as cur:
            for statement in MYSQL_SCHEMA:
                cur.execute(statement)


def _decode(row_payload):
    if isinstance(row_payload, (bytes, bytearray)):
        row_payload = row_payload.decode("utf-8")
    return json.loads(row_payload)


def init():
    """建表 + 首次导入目录数据 + 迁移旧 JSON 记忆库。"""
    with _lock:
        driver, conn, _ = connect()
        _ensure_schema(driver, conn)
        total = count_products(driver, conn)
        if total == 0 and os.path.exists(CATALOG_PATH):
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                catalog = json.load(f)
            _upsert(driver, conn, catalog)
        if driver == "sqlite" and count_memory(driver, conn) == 0 and os.path.exists(LEGACY_MEMORY):
            try:
                with open(LEGACY_MEMORY, "r", encoding="utf-8") as f:
                    legacy = json.load(f)
                for uid, profile in legacy.items():
                    save_memory(uid, profile)
            except (json.JSONDecodeError, OSError):
                pass
        conn.close()


def count_products(driver=None, conn=None):
    own = conn is None
    if own:
        driver, conn, _ = connect()
    try:
        if driver == "sqlite":
            cur = conn.execute("SELECT COUNT(*) FROM products")
        else:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM products")
        return int(cur.fetchone()[0])
    finally:
        if own:
            conn.close()


def count_memory(driver=None, conn=None):
    own = conn is None
    if own:
        driver, conn, _ = connect()
    try:
        if driver == "sqlite":
            cur = conn.execute("SELECT COUNT(*) FROM memory")
        else:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM memory")
        return int(cur.fetchone()[0])
    finally:
        if own:
            conn.close()


def load_products(origin=None):
    driver, conn, _ = connect()
    try:
        sql = "SELECT payload FROM products"
        args = ()
        if origin:
            sql += " WHERE origin = " + ("?" if driver == "sqlite" else "%s")
            args = (origin,)
        cur = conn.execute(sql, args) if driver == "sqlite" else conn.cursor()
        if driver != "sqlite":
            cur.execute(sql, args)
        rows = cur.fetchall()
        items = []
        for row in rows:
            try:
                items.append(_decode(row[0]))
            except (json.JSONDecodeError, TypeError):
                continue
        items.sort(key=lambda p: (p.get("category") or "", -float(p.get("price") or 0)))
        return items
    finally:
        conn.close()


def load_product(product_id):
    driver, conn, _ = connect()
    try:
        sql = "SELECT payload FROM products WHERE id = " + ("?" if driver == "sqlite" else "%s")
        if driver == "sqlite":
            cur = conn.execute(sql, (product_id,))
        else:
            cur = conn.cursor()
            cur.execute(sql, (product_id,))
        row = cur.fetchone()
        return _decode(row[0]) if row else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None
    finally:
        conn.close()


def _upsert(driver, conn, items):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item in items:
        if not item.get("id"):
            continue
        payload = json.dumps(item, ensure_ascii=False)
        if driver == "sqlite":
            conn.execute(
                """INSERT INTO products (id, name, brand, category, brand_tier, price, origin, payload, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, brand=excluded.brand, category=excluded.category,
                     brand_tier=excluded.brand_tier, price=excluded.price, origin=excluded.origin,
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (item["id"], item.get("name", ""), item.get("brand", ""), item.get("category", ""),
                 item.get("brandTier", ""), float(item.get("price") or 0), item.get("origin", "seed"),
                 payload, now),
            )
        else:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO products (id, name, brand, category, brand_tier, price, origin, payload, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                         name=VALUES(name), brand=VALUES(brand), category=VALUES(category),
                         brand_tier=VALUES(brand_tier), price=VALUES(price), origin=VALUES(origin),
                         payload=VALUES(payload), updated_at=VALUES(updated_at)""",
                    (item["id"], item.get("name", ""), item.get("brand", ""), item.get("category", ""),
                     item.get("brandTier", ""), float(item.get("price") or 0), item.get("origin", "seed"),
                     payload, now),
                )
    if driver == "sqlite":
        conn.commit()


def upsert_products(items):
    if not items:
        return 0
    with _lock:
        driver, conn, _ = connect()
        try:
            _upsert(driver, conn, items)
            return len(items)
        finally:
            conn.close()


def load_memory():
    driver, conn, _ = connect()
    try:
        cur = conn.execute("SELECT user_id, payload FROM memory") if driver == "sqlite" else conn.cursor()
        if driver != "sqlite":
            cur.execute("SELECT user_id, payload FROM memory")
        memory = {}
        for uid, payload in cur.fetchall():
            try:
                memory[uid] = _decode(payload)
            except (json.JSONDecodeError, TypeError):
                continue
        return memory
    finally:
        conn.close()


def save_memory(user_id, profile):
    payload = json.dumps(profile, ensure_ascii=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        driver, conn, _ = connect()
        try:
            if driver == "sqlite":
                conn.execute(
                    """INSERT INTO memory (user_id, payload, updated_at) VALUES (?, ?, ?)
                       ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at""",
                    (user_id, payload, now),
                )
                conn.commit()
            else:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO memory (user_id, payload, updated_at) VALUES (%s, %s, %s)
                           ON DUPLICATE KEY UPDATE payload=VALUES(payload), updated_at=VALUES(updated_at)""",
                        (user_id, payload, now),
                    )
        finally:
            conn.close()


def delete_memory(user_id):
    with _lock:
        driver, conn, _ = connect()
        try:
            if driver == "sqlite":
                conn.execute("DELETE FROM memory WHERE user_id = ?", (user_id,))
                conn.commit()
            else:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM memory WHERE user_id = %s", (user_id,))
        finally:
            conn.close()


def info():
    return {
        "backend": _STATE.get("backend") or "sqlite",
        "configured": _conf()[0],
        "warning": _STATE.get("warning", ""),
        "sqlitePath": _display_path(SQLITE_PATH),
    }


def _display_path(path):
    """展示用路径：能相对化就相对化，跨盘符时退回绝对路径。"""
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:
        return path
