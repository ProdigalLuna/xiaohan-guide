# -*- coding: utf-8 -*-
"""浏览器端 API 桥（Pyodide）。

与 server.py 的路由一一对应，但不依赖 http.server / socket，
由 js/pyapi.js 通过 Pyodide 调用本模块的 handle_json(method, target, body)。
"""

import base64
import json
import os
import re
import sys
import urllib.parse

os.environ.setdefault("APP_WRITE_DIR", "/tmp_write")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import engine  # noqa: E402

JSON_CT = "application/json; charset=utf-8"
XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _split(target):
    parsed = urllib.parse.urlparse(target)
    return parsed.path.rstrip("/") or "/", urllib.parse.parse_qs(parsed.query)


def _first(params, key, default=""):
    values = params.get(key) or []
    return values[0] if values else default


def _ok(obj, status=200):
    return {
        "status": status,
        "content_type": JSON_CT,
        "binary": False,
        "body": json.dumps(obj, ensure_ascii=False),
    }


def _text(body, status=200, content_type="text/plain; charset=utf-8"):
    return {"status": status, "content_type": content_type, "binary": False, "body": body}


def _download(name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.xlsx", name or ""):
        return _ok({"error": "bad file name"}, 400)
    full = os.path.join(engine.EXPORT_DIR, name)
    if not os.path.isfile(full):
        return _text("Not Found", 404)
    with open(full, "rb") as f:
        data = f.read()
    return {
        "status": 200,
        "content_type": XLSX_CT,
        "binary": True,
        "body": base64.b64encode(data).decode("ascii"),
        "filename": name,
    }


def handle(method, target, body_text):
    """返回 dict：{status, content_type, binary, body[, filename]}"""
    method = (method or "GET").upper()
    path, query = _split(target)
    try:
        payload = json.loads(body_text) if body_text else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    uid = payload.get("userId") or "anon"

    if method == "GET":
        if path == "/api/config":
            return _ok(engine.config_info())
        if path == "/api/products":
            brand = _first(query, "brand")
            scene = _first(query, "scene")
            uid = _first(query, "userId", "anon")
            profile = engine.get_profile(uid)
            items = engine._all_products()
            if brand:
                items = [p for p in items if p["brand"] == brand]
            if scene:
                items = [p for p in items if scene in p.get("scenes", [])]
            return _ok({
                "products": [engine.product_card(p, profile) for p in items],
                "total": len(items),
            })
        if path == "/api/recommend":
            uid = _first(query, "userId", "anon")
            limit = int(_first(query, "limit", "6"))
            return _ok(engine.recommend_feed(uid, limit=limit))
        if path == "/api/memory":
            uid = _first(query, "userId", "anon")
            profile = engine.get_profile(uid)
            return _ok({"profile": engine.profile_summary(profile), "raw": profile})
        if path == "/api/product":
            pid = _first(query, "id")
            uid = _first(query, "userId", "anon")
            detail = engine.product_detail(pid, engine.get_profile(uid))
            if not detail:
                return _ok({"error": "not found"}, 404)
            return _ok(detail)
        if path.startswith("/download/"):
            return _download(path[len("/download/"):])
        if path.startswith("/api/"):
            return _ok({"error": "unknown endpoint"}, 404)
        return _text("Not Found", 404)

    if method == "POST":
        if path == "/api/chat":
            message = (payload.get("message") or "").strip()
            if not message:
                return _ok({"error": "message 不能为空"}, 400)
            return _ok(engine.handle_chat(uid, message, payload.get("dialect")))
        if path == "/api/compare":
            ids = payload.get("ids") or []
            if len(ids) < 2:
                return _ok({"error": "至少需要 2 个商品"}, 400)
            parsed = {"scenes": payload.get("scenes") or [], "brands": [], "intent": "compare"}
            profile = engine.get_profile(uid)
            result = engine.build_compare(ids, profile, parsed)
            for pid in result["ids"]:
                engine.record_event(uid, "compare", pid)
            engine._remember_compare(uid, engine._compare_export_model(result), result["ids"])
            return _ok({"compare": result})
        if path == "/api/event":
            product_id = payload.get("productId")
            if not product_id:
                return _ok({"error": "productId 不能为空"}, 400)
            profile = engine.record_event(uid, payload.get("type") or "view", product_id)
            return _ok({"ok": True, "profile": engine.profile_summary(profile)})
        if path == "/api/settings":
            profile = engine.set_setting(uid, "avoidNew", bool(payload.get("avoidNew", True)))
            return _ok({"ok": True, "profile": engine.profile_summary(profile)})
        if path == "/api/reset":
            engine.reset_memory(uid)
            return _ok({"ok": True})
        return _ok({"error": "unknown endpoint"}, 404)

    if method == "DELETE":
        if path == "/api/memory":
            engine.reset_memory(_first(query, "userId", "anon"))
            return _ok({"ok": True})
        return _ok({"error": "unknown endpoint"}, 404)

    return _ok({"error": "method not allowed"}, 405)


def handle_json(method, target, body_text):
    """供 JS 调用的入口：始终返回 JSON 字符串。"""
    try:
        result = handle(method, target, body_text)
    except Exception as exc:  # noqa: BLE001
        result = _ok({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
    return json.dumps(result, ensure_ascii=False)


def warmup():
    """预热：让首次对话不至于等待太久。"""
    try:
        engine.config_info()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return "warmup failed: %s" % exc


# JS 侧通过 __main__ 全局读取
bridge_handle = handle_json
