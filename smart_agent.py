# -*- coding: utf-8 -*-
"""智能导购增强层。

1. 识别淘宝/京东链接后，静默调用搜索引擎补全机型参数，只展示结果，不展示搜索过程；
2. 将链接商品登记为可对比商品，能与站内机型沿用原对比表进行比较；
3. 普通对话仍走原引擎，仅同步文案口径为“智能产品”。
"""

import hashlib
import html
import re
from datetime import datetime

import db
import engine
import linkparse
import websearch

SMART_CATEGORIES = set(db.SMART_CATEGORIES)


def _patch_reply(res):
    """普通对话的文案同步为智能产品口径，不改变原有结构与展现。"""
    if not isinstance(res, dict):
        return res
    text = res.get("reply")
    if text:
        text = text.replace(
            "手机、电脑、耳机、家电、美妆、食品都能挑",
            "手机、电脑、平板、耳机、智能穿戴、智能家电都能挑，为您的智能产品保驾护航",
        )
        text = text.replace(
            "手机 / 笔记本 / 耳机 / 家电 / 美妆…",
            "手机 / 笔记本 / 平板 / 耳机 / 智能穿戴 / 家电…",
        )
        res["reply"] = text
    return res


def _recover_title(info, url, platform):
    """商品页与商品 ID 反查都失败时，用链接本身再向搜索引擎取一次标题。"""
    item_id = info.get("itemId") or ""
    queries = []
    if item_id:
        queries.append(f"{item_id} 商品 型号 参数")
    label = {"jd": "京东", "taobao": "淘宝"}.get(platform, "电商")
    queries.append(f"{label} 商品链接 {url}")
    for query in queries:
        try:
            results, _provider = websearch.search(query, n=5)
        except Exception:
            continue
        for item in results or []:
            title = linkparse._clean_title(item.get("title", ""))
            if len(title) >= 6 and not linkparse._is_seo_blob(title):
                return title
    return ""


def _build_virtual(info, linked):
    """把链接识别结果登记成可对比商品（参数来自公开搜索结果）。"""
    title = (info.get("title") or "").strip()
    if not title:
        return None

    key = (info.get("finalUrl") or info.get("url") or title).strip()
    pid = "link-" + hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    specs = dict(info.get("specs") or {})
    price = info.get("price") or (linked or {}).get("price") or 0

    category = engine._link_category(title, linked) or (linked or {}).get("category") or "手机"
    if category not in SMART_CATEGORIES:
        category = "手机"

    brand_list = engine.parse_brands(title)
    brand = ("、".join(brand_list) if brand_list
             else (linked or {}).get("brand") or title[:2])
    color, color2 = websearch.BRAND_COLORS.get(brand, ("#5b6b8c", "#9aa8c2"))

    try:
        scenes = engine.parse_query(title).get("scenes") or []
    except Exception:
        scenes = []
    if not scenes and linked:
        scenes = linked.get("scenes") or []

    highlights = [f"{k} {v}" for k, v in list(specs.items())[:3]]
    if not highlights:
        highlights = ["公开搜索结果已整理，参数以商品页为准"]

    product = {
        "id": pid,
        "name": title[:60],
        "brand": brand,
        "brandTier": "popular",
        "category": category,
        "price": price,
        "rating": None,
        "sales": None,
        "tags": ["链接解析", "搜索补全", category][:3],
        "highlights": highlights,
        "audience": "关注该机型的用户",
        "scenes": scenes[:4],
        "environments": ["indoor", "outdoor"],
        "specs": specs,
        "color": color,
        "color2": color2,
        "origin": "web",
        "estimated": True,
        "sourceUrl": info.get("finalUrl") or info.get("url") or "",
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "note": "参数来自公开搜索结果，价格与配置以商品页为准",
    }
    db.upsert_products([product])
    return product


def _spec_text(info, anchor, title):
    return (engine._info_specs_text(info)
            or engine._short_specs(anchor)
            or engine._title_specs(title)
            or "【暂无公开参数】")


def _markdown_table(anchor, info, competitors, title):
    """沿用原版表格：商品 / 价格 / 核心配置 / 主要优势 / 短板 / 适合人群。"""
    lines = ["| 商品 | 价格 | 核心配置 | 主要优势 | 短板 | 适合人群 |",
             "|---|---|---|---|---|---|"]

    table_products = ([anchor] if anchor else []) + competitors
    table_view = []
    for p in table_products:
        item = dict(p)
        if anchor and info.get("price") and p.get("id") == anchor.get("id"):
            item["price"] = info["price"]
        table_view.append(item)

    spec_text = _spec_text(info, anchor, title)
    if anchor:
        name_cell = f"{anchor['name']}（原商品）"
        strengths = "；".join((anchor.get("highlights") or [])[:2]) or "【暂无公开参数】"
        audience = anchor.get("audience") or "【暂无公开参数】"
    else:
        name_cell = (title[:42] + "…" if len(title) > 42 else title) or "原商品"
        name_cell += "（原商品）"
        strengths = "；".join(f"{k} {v}" for k, v in list((info.get("specs") or {}).items())[:2]) or "【暂无公开参数】"
        audience = "【暂无公开参数】"

    row = [
        name_cell,
        engine._price_cell(anchor, info.get("price"), original=True),
        spec_text,
        strengths,
        engine._weak_points(anchor, table_view),
        audience,
    ]
    lines.append("| " + " | ".join(engine._md_cell(v) for v in row) + " |")

    for p in competitors:
        strengths = ("；".join(p.get("highlights", [])[:2])
                     or "；".join(p.get("tags", [])[:2])
                     or "【暂无公开参数】")
        row = [
            p["name"],
            engine._price_cell(p),
            engine._short_specs(p) or "【暂无公开参数】",
            strengths,
            engine._weak_points(p, table_view),
            p.get("audience") or "【暂无公开参数】",
        ]
        lines.append("| " + " | ".join(engine._md_cell(v) for v in row) + " |")
    return lines


def _title_from_search_text(text, item_id):
    """从浏览器抓来的搜索页文本里挑出机型标题。"""
    if not text:
        return ""
    candidates = []
    for line in re.split(r"[\r\n]+", text):
        line = linkparse._plain(line)
        if 8 <= len(line) <= 120 and not linkparse._is_seo_blob(line):
            candidates.append(line)
    # 优先含商品 ID 的行
    if item_id:
        for line in candidates:
            if item_id in line:
                cleaned = linkparse._clean_title(line)
                if len(cleaned) >= 6:
                    return cleaned
    # 其次含常见机型关键词的行
    keywords = ("Ultra", "Pro", "Max", "Plus", "手机", "骁龙", "天玑", "iPhone", "Mate")
    for line in candidates:
        if any(k.lower() in line.lower() for k in keywords):
            cleaned = linkparse._clean_title(line)
            if len(cleaned) >= 6:
                return cleaned
    return ""


def _apply_net(info, net, platform):
    """把浏览器抓来的商品页/搜索文本解析成 title/price/specs，补进 info。"""
    if not net or not isinstance(net, dict):
        return info
    page = net.get("page") or ""
    search = net.get("search") or ""
    text = net.get("text") or ""          # 云端已转成纯文本的商品页正文
    cloud_title = net.get("title") or ""  # 云端已清洗的真实商品标题
    item_id = net.get("itemId") or ""

    # 标题：云端 title 优先（服务端抓取，可信），其次从页面 meta 提取。
    # Pyodide 内 parse_link 联网必失败，它的 title 可能来自兜底，不可信，一律覆盖。
    net_title = linkparse._clean_title(html.unescape(cloud_title)) if cloud_title else ""
    if not net_title and page:
        og = linkparse._meta(page, ["og:title", "twitter:title"])
        if not og:
            m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
            og = re.sub(r"<[^>]+>", "", m.group(1)) if m else ""
        net_title = linkparse._clean_title(html.unescape(og))
    if linkparse._valid_title(net_title):
        info["title"] = net_title
        info["source"] = "商品页"
    if page and not info.get("price"):
        info["price"] = linkparse._price_from(page)
    if page and not info.get("spec"):
        desc = linkparse._meta(page, ["description", "og:description"])
        if desc and len(desc) >= 8 and not linkparse._is_seo_blob(desc):
            info["spec"] = desc[:180]

    # 参数来源优先级：商品页纯文本 > 搜索文本 > 商品页 HTML > 标题
    blob = text or search or page
    if not info.get("title") and blob:
        title = _title_from_search_text(blob, item_id)
        if title:
            info["title"] = title
            info["source"] = "搜索引擎"
    if not info.get("price"):
        # 宽松匹配：从搜索文本/描述里找“6499元”“到手价6499”等
        for source in (search, page):
            if not source:
                continue
            desc = linkparse._meta(source, ["description", "og:description"]) or ""
            blob = source + " " + desc
            m = re.search(r"(?<!\d)([1-9]\d{2,5})\s*元(?!\d)", blob)
            if m:
                info["price"] = float(m.group(1))
                break
    if info.get("title") and not info.get("specs"):
        # 逐个来源尝试，取参数最全的那份
        best = {}
        for src in (text, search, page, info["title"]):
            if not src:
                continue
            specs = linkparse._specs_from_text(src)
            if len(specs) > len(best):
                best = specs
        if best:
            info["specs"] = linkparse._order_specs(best)
            info["specSource"] = "公开页面"
            info["spec"] = linkparse.format_specs(info["specs"])
    return info


def handle_link(user_id, message, url, platform, net=None):
    """链接入口：识别商品 → 静默搜索补全 → 展示结果 → 支持原样对比。"""
    info = linkparse.parse_link(url)
    info = _apply_net(info, net, platform)
    title = (info.get("title") or "").strip()
    if not title:
        title = _recover_title(info, url, platform)
        if title:
            info["title"] = title
            specs, spec_source = linkparse.enrich_specs(title)
            info["specs"] = specs
            info["spec"] = linkparse.format_specs(specs)
            info["specSource"] = spec_source

    linked = engine._match_link_product(title) if title else None
    profile = engine.get_profile(user_id)

    anchor = _build_virtual(info, linked) if title else None
    base = anchor or linked
    category = engine._link_category(title, linked or anchor) if title else ""
    if category not in SMART_CATEGORIES:
        category = (anchor or {}).get("category") or "手机"

    anchor_price = info.get("price") or (base or {}).get("price") or None
    competitors = engine._pick_competitors(base, category, anchor_price, limit=4) if category else []

    exclude = set()
    if anchor:
        exclude.add(anchor["id"])
    if linked:
        exclude.add(linked["id"])
    competitors = [p for p in competitors if p["id"] not in exclude]

    brand = "、".join(engine.parse_brands(title)) if title else ""
    if not brand and linked:
        brand = linked.get("brand") or ""

    lines = ["**① 商品基础信息**", ""]
    display_name = title or (linked or {}).get("name") or "【暂无公开参数】"
    lines.append(f"- 商品名称：{display_name}")
    lines.append(f"- 品牌 / 型号：{brand or '【暂无公开参数】'} / {(linked or {}).get('name') or title or '【暂无公开参数】'}")
    lines.append(f"- 价格：{('¥%g' % info['price']) if info.get('price') else '【暂无公开参数】（页面价格需登录或动态加载，未能抓取）'}")
    lines.append(f"- 平台：{info.get('platform') or platform or '电商平台'}")
    lines.append(f"- 购买链接：{info.get('finalUrl') or url}")
    lines.append(f"- 所属品类：{category}")
    lines.append(f"- 核心参数：{_spec_text(info, anchor, title)}")
    # 不展示“解析来源 / 搜索引擎 / 参数来源”等过程信息

    lines.append("")
    lines.append(f"**② 同价位同类型竞品（{len(competitors)} 款）**")
    lines.append("")
    if not competitors:
        lines.append("站内暂无可比机型。")
    else:
        lines.extend(_markdown_table(anchor, info, competitors, title))

    lines.append("")
    lines.append("**③ 选购总结建议**")
    lines.append("")
    verdict_lines = engine._link_verdict(anchor or linked, info.get("price"), competitors)
    lines.extend(verdict_lines)
    lines.append("")
    lines.append("> 说明：以上数据均来自公开页面与搜索结果，实际价格与配置以商品页为准。")

    compare_ids = ([anchor["id"]] if anchor else []) + [p["id"] for p in competitors]
    compare = (engine.build_compare(compare_ids, profile, {"scenes": []})
               if len(compare_ids) >= 2 else None)

    if compare:
        for pid in compare.get("ids", []):
            engine.record_event(user_id, "compare", pid)
        engine._remember_compare(user_id, engine._compare_export_model(compare), compare.get("ids"))
    for p in competitors:
        engine.record_event(user_id, "view", p["id"])

    cards = ([anchor] if anchor else []) + competitors
    quicks = []
    if compare:
        quicks.append(f"对比这 {len(compare_ids)} 款")
    if anchor:
        quicks.append("看看原商品详情")
    if competitors:
        quicks.append("再找几款更便宜的")

    return {
        "reply": "\n".join(lines),
        "markdown": True,
        "narrator": "template",
        "intent": "link",
        "dialect": None,
        "parsed": {
            "normalized": message,
            "budget": None,
            "brands": [brand] if brand else [],
            "scenes": [],
            "mentions": [anchor["name"]] if anchor else [],
        },
        "products": [engine.product_card(p, profile) for p in cards],
        "compare": compare,
        "plan": None,
        "quickReplies": quicks,
        "profile": engine.profile_summary(profile),
    }


def handle_chat(user_id, message, dialect_hint=None, net=None):
    url, platform = linkparse.find_link(message or "")
    if url:
        return handle_link(user_id, message, url, platform, net=net)
    return _patch_reply(engine.handle_chat(user_id, message, dialect_hint))
