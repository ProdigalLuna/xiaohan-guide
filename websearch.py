# -*- coding: utf-8 -*-
"""联网商品搜索：360/Bing 网页搜索（免密钥，可选 Tavily/Serper）→ 大模型抽取结构化商品事实。

只作为补充信源：本地商品库仍是主数据，联网结果标记 origin=web、价格标注“参考价”。
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "llm_config.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_lock = threading.RLock()
_CACHE = {}
_TTL = 900  # 同一查询 15 分钟内不重复联网

import db

BRAND_COLORS = {
    "小米": ("#ff6900", "#ffb26b"),
    "红米": ("#f04a23", "#ff8f6b"),
    "苹果": ("#3a3f4b", "#7d8497"),
    "华为": ("#c8102e", "#ff6b7f"),
    "荣耀": ("#0aa1e0", "#69d0ff"),
    "OPPO": ("#0f8f5f", "#5cd3a4"),
    "vivo": ("#2b6cff", "#7ea6ff"),
    "一加": ("#e02a2a", "#ff7b7b"),
    "三星": ("#1f4cbf", "#6f93ff"),
}

TIER_MAP = {"高奢": "luxury", "热门": "popular", "经济": "economy", "小众": "niche",
            "luxury": "luxury", "popular": "popular", "economy": "economy", "niche": "niche"}

SCENE_HINTS = {
    "拍照": ["影像", "拍照", "相机", "长焦", "夜景", "徕卡", "蔡司", "哈苏"],
    "游戏": ["游戏", "性能", "散热", "高刷", "电竞", "骁龙", "天玑"],
    "续航": ["续航", "电池", "快充", "青海湖"],
    "小屏": ["小屏", "单手", "轻巧"],
    "大屏": ["大屏", "6.7", "6.8"],
    "轻薄": ["轻薄", "轻至", "毫米", "克"],
    "性价比": ["性价比", "千元", "亲民", "白菜"],
    "折叠": ["折叠", "flip", "fold"],
    "商务": ["商务", "办公", "旗舰"],
    "自拍": ["自拍", "前置"],
    "系统": ["系统", "iOS", "鸿蒙", "纯净"],
    "信号": ["信号", "卫星", "北斗"],
    "音乐": ["音质", "LDAC", "Hi-Res", "音频"],
    "通勤": ["通勤", "降噪", "便携"],
    "健康": ["心率", "血氧", "睡眠", "监测"],
    "居家": ["家用", "家居", "客厅", "厨房", "清洁"],
    "护肤": ["护肤", "精华", "保湿", "修护"],
    "运动": ["运动", "跑步", "骑行", "健身"],
    "户外": ["户外", "露营", "徒步", "防水"],
    "零食": ["零食", "坚果", "酸奶", "冲泡"],
}

# ---------------------------------------------------------------- 配置
def _conf():
    conf = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except (json.JSONDecodeError, OSError):
            conf = {}
    key = (os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("OPENAI_API_KEY") or conf.get("apiKey") or "")
    base = (os.environ.get("LLM_API_BASE") or conf.get("apiBase") or "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL") or conf.get("model") or "deepseek-chat"
    return key, base, model, conf.get("webSearch", {}) or {}


def enabled():
    """联网检索至少有一条可用路径：免费 Bing，或配置了 Tavily/Serper 的 Key。"""
    return True


# ---------------------------------------------------------------- 联网商品存储（统一走数据库）
def all_products():
    return db.load_products(origin="web")


def get(product_id):
    product = db.load_product(product_id)
    return product if product and product.get("origin") == "web" else None


def _register(products):
    """写入联网商品库；已存在同名商品则复用旧记录，保证重复查询也能返回结果。"""
    if not products:
        return []
    existing = {p["name"]: p for p in all_products()}
    result, new_items = [], []
    for p in products:
        if p["name"] in existing:
            result.append(existing[p["name"]])
            continue
        new_items.append(p)
        result.append(p)
    if new_items:
        db.upsert_products(new_items)
    return result


# ---------------------------------------------------------------- 网页搜索
def _fetch(url, timeout=10, data=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _strip(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
                .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", text).strip()


def _bing(query, n=6):
    url = ("https://cn.bing.com/search?q=" + urllib.parse.quote(query)
           + "&count=10&setlang=zh-CN&mkt=zh-CN")
    html = _fetch(url, timeout=10)
    items = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
        m = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        href, title = m.group(1), _strip(m.group(2))
        sm = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = _strip(sm.group(1)) if sm else ""
        if title:
            items.append({"title": title[:120], "snippet": snippet[:400], "url": href[:300]})
        if len(items) >= n:
            break
    return items


def _so360(query, n=6):
    html = _fetch("https://www.so.com/s?q=" + urllib.parse.quote(query) + "&pn=1", timeout=10)
    starts = [m.start() for m in re.finditer(r'<li[^>]+class="[^"]*res-list[^"]*"', html)]
    items = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else start + 2600
        block = html[start:end]
        m = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        if not m:
            continue
        title = _strip(m.group(1))[:120]
        if not title or "搜了" in title:
            continue
        body = _strip(block)
        if body.startswith(title):
            body = body[len(title):].strip()
        href = re.search(r'href="(http[^"]+)"', block)
        items.append({"title": title, "snippet": body[:400], "url": href.group(1)[:300] if href else ""})
        if len(items) >= n:
            break
    return items


def _tavily(query, n=6):
    key, _, _, _ = _conf()
    if not key:
        return []
    payload = {"api_key": key, "query": query, "max_results": n, "search_depth": "basic"}
    raw = _fetch("https://api.tavily.com/search", timeout=15,
                 data=json.dumps(payload).encode("utf-8"),
                 headers={"Content-Type": "application/json"})
    data = json.loads(raw)
    return [{"title": r.get("title", "")[:120], "snippet": (r.get("content") or "")[:400],
             "url": r.get("url", "")[:300]} for r in data.get("results", [])][:n]


def _serper(query, n=6):
    _, _, _, ws = _conf()
    key = ws.get("apiKey") or ""
    if not key:
        return []
    raw = _fetch("https://google.serper.dev/search", timeout=15,
                 data=json.dumps({"q": query, "num": n}).encode("utf-8"),
                 headers={"Content-Type": "application/json", "X-API-KEY": key})
    data = json.loads(raw)
    return [{"title": r.get("title", "")[:120], "snippet": (r.get("snippet") or "")[:400],
             "url": r.get("link", "")[:300]} for r in data.get("organic", [])][:n]


def _relevance(items, query):
    """判断搜索结果是否真的和查询主体（通常是机型名）相关。"""
    head = (query or "").split(" ")[0]
    head = re.sub(r"\s", "", head).lower()
    if not head:
        return 0
    hits = 0
    for item in items:
        text = (item["title"] + item["snippet"]).replace(" ", "").lower()
        if head in text:
            hits += 1
    return hits


def search(query, n=6):
    _, _, _, ws = _conf()
    provider = ws.get("provider") or "auto"
    if provider == "auto":
        order = ["so360", "bing", "tavily", "serper"]
    else:
        order = {"so360": ["so360", "bing"], "bing": ["bing", "so360"],
                 "tavily": ["tavily", "so360", "bing"],
                 "serper": ["serper", "so360", "bing"]}.get(provider, ["so360", "bing"])
    best = None
    for name in order:
        try:
            items = {"so360": _so360, "bing": _bing, "tavily": _tavily, "serper": _serper}[name](query, n)
        except Exception:
            continue
        if not items:
            continue
        rel = _relevance(items, query)
        if best is None or rel > best[1]:
            best = (items, rel, name)
        if rel >= 2:
            return items, name
    if best:
        return best[0], best[2]
    return [], None


# ---------------------------------------------------------------- 大模型抽取
def _llm_json(system, user, max_tokens=900, timeout=20):
    key, base, model, _ = _conf()
    if not key:
        return None
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return json.loads(data["choices"][0]["message"]["content"])
    except Exception:
        return None


EXTRACT_SYSTEM = (
    "你是商品信息抽取器。根据搜索摘要抽取 1-3 个真实在售的同品类商品，只输出 JSON："
    '{"products":[{"name":"完整型号名","category":"手机/笔记本/平板/耳机/智能穿戴/家电/美妆/食品/服饰/家居/其他",'
    '"brand":"品牌名","brandTier":"高奢/热门/经济/小众","price":整数元,'
    '"tags":["最多4个短标签"],"highlights":["最多3条亮点"],"audience":"一句话适合人群",'
    '"environments":["室内","室外"],"specs":{"参数名":"参数值"}}]}。'
    "规则：价格必须是摘要中出现的整数人民币；摘要没提到的字段给空数组/空对象；不要臆造未出现的型号；"
    "摘要里没有明确价格时返回 {\"products\":[]}。"
)


def _infer_scenes(text):
    hits = []
    for scene, words in SCENE_HINTS.items():
        if any(w.lower() in text.lower() for w in words):
            hits.append(scene)
    return hits[:4] or ["均衡"]


def _sanitize(raw, source_urls):
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:40]
    if len(name) < 3:
        return None
    price_text = re.sub(r"[^\d]", "", str(raw.get("price") or ""))
    if not price_text:
        return None
    price = int(price_text)
    if not (50 <= price <= 60000):
        return None
    brand = str(raw.get("brand") or "").strip() or name[:2]
    category = str(raw.get("category") or "").strip()[:16] or "其他"
    tier = TIER_MAP.get(str(raw.get("brandTier") or "").strip(), "popular")
    tags = [str(t).strip()[:8] for t in (raw.get("tags") or []) if str(t).strip()][:4]
    highlights = [str(h).strip()[:40] for h in (raw.get("highlights") or []) if str(h).strip()][:3]
    if not tags:
        tags = ["联网新品", "参考价"]
    if not highlights:
        highlights = ["来自联网检索，详情以官方页面为准"]
    audience = str(raw.get("audience") or "").strip()[:60] or "关注该商品的用户"

    env_map = {"室内": "indoor", "室外": "outdoor", "indoor": "indoor", "outdoor": "outdoor"}
    envs = [env_map[e] for e in (raw.get("environments") or []) if e in env_map]
    envs = list(dict.fromkeys(envs)) or ["indoor", "outdoor"]

    raw_specs = raw.get("specs") if isinstance(raw.get("specs"), dict) else {}
    specs = {str(k)[:20]: str(v)[:40] for k, v in raw_specs.items() if v}
    specs = dict(list(specs.items())[:6]) or {"类型": category}

    color, color2 = BRAND_COLORS.get(brand, ("#5b6b8c", "#9aa8c2"))
    pid = "web-" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    text = name + " ".join(tags) + " ".join(highlights) + audience
    return {
        "id": pid,
        "name": name,
        "brand": brand,
        "brandTier": tier,
        "category": category,
        "price": price,
        "rating": None,
        "sales": None,
        "tags": tags,
        "highlights": highlights,
        "audience": audience,
        "scenes": _infer_scenes(text),
        "environments": envs,
        "specs": specs,
        "color": color,
        "color2": color2,
        "origin": "web",
        "estimated": True,
        "sourceUrl": (source_urls[0] if source_urls else ""),
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "note": "联网检索的参考价，实际以官方/电商页面为准",
    }


def extract_products(query, results):
    if not results:
        return []
    facts = [{"title": r["title"], "snippet": r["snippet"]} for r in results[:6]]
    data = _llm_json(EXTRACT_SYSTEM, f"用户查询：{query}\n搜索结果：{json.dumps(facts, ensure_ascii=False)}")
    if not data:
        return []
    urls = [r["url"] for r in results if r.get("url")]
    out = []
    for raw in (data.get("products") or [])[:3]:
        item = _sanitize(raw, urls)
        if item:
            out.append(item)
    return out


def lookup(query, force=False):
    """搜索 + 抽取，返回新增的联网商品。失败时返回 []。"""
    query = (query or "").strip()
    if not query:
        return []
    now = time.time()
    with _lock:
        hit = _CACHE.get(query)
        if hit and not force and now - hit[0] < _TTL:
            return hit[1]

    results, _provider = search(query)
    products = extract_products(query, results) if results else []
    added = _register(products)
    with _lock:
        _CACHE[query] = (now, added)
    return added


def stats():
    key, _, _, ws = _conf()
    return {
        "enabled": True,
        "provider": ws.get("provider") or "auto(so360→bing)",
        "keyConfigured": bool(ws.get("apiKey") or key),
        "cachedProducts": len(all_products()),
        "cacheSize": len(_CACHE),
    }
