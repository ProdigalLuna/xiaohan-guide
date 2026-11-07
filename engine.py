# -*- coding: utf-8 -*-
"""智能导购核心引擎

能力：
1. 方言归一化 + 中文数字解析 + 需求抽取（预算/品牌/场景/具体机型）
2. 混合检索引擎（规则 + TF-IDF 语义向量 + 大模型查询理解 + 联网商品补充）
3. 价格 / 好评 / 销量 对比表生成
4. 用户记忆库（历史搜索、浏览、点击、偏好权重）
5. 可选大模型解说层（配置 LLM_API_KEY 后启用，失败自动回落模板）
"""

import math
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime

import websearch
import linkparse
import xlsx_export
import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

_lock = threading.RLock()

# 运行时大模型配置：只由前端在本机注入（存浏览器 localStorage），不写入仓库与磁盘
RUNTIME_LLM = {"apiKey": "", "apiBase": "", "model": ""}

db.init()
PRODUCTS = db.load_products()
with open(os.path.join(DATA_DIR, "dialects.json"), "r", encoding="utf-8") as f:
    DIALECTS = json.load(f)

PRODUCT_BY_ID = {p["id"]: p for p in PRODUCTS}


def web_products():
    return db.load_products(origin="web")


def _all_products():
    return db.load_products()


def product_by_id(pid):
    return db.load_product(pid) or PRODUCT_BY_ID.get(pid)

# ---------------------------------------------------------------- 机型别名
EXTRA_ALIASES = {
    "mi15": ["小米15", "米15", "mi15", "小米十五", "小屏旗舰"],
    "mi15pro": ["小米15pro", "小米十五pro", "mi15pro", "15pro", "小米 15 pro"],
    "mi14": ["小米14", "米14", "mi14"],
    "k80": ["红米k80", "k80", "redmi k80"],
    "turbo4": ["红米turbo4", "turbo4", "turbo 4", "红米turbo"],
    "mixflip": ["mix flip", "mixflip", "小米折叠", "小折叠"],
    "ip16": ["iphone16", "苹果16", "ip16", "苹果十六", "ipone16"],
    "ip16pro": ["iphone16pro", "苹果16pro", "16pro", "苹果十六pro"],
    "ip15": ["iphone15", "苹果15", "ip15"],
    "ip16plus": ["iphone16plus", "苹果16plus", "16plus"],
    "mate70": ["mate70", "华为mate70", "mate 70", "mate70pro"],
    "nova13": ["nova13", "华为nova13", "nova 13"],
    "pura70": ["pura70", "华为pura70", "pura 70"],
    "magic7": ["magic7", "荣耀magic7", "magic 7"],
    "findx8": ["findx8", "find x8", "oppo findx8", "findx8pro"],
    "x200": ["x200", "vivo x200", "vivox200"],
    "oneplus13": ["一加13", "oneplus13", "加13", "一加十三"],
    "s25": ["s25", "galaxy s25", "三星s25", "s25ultra"],
}


def _build_alias_index():
    idx = {}
    for p in PRODUCTS:
        for alias in [p["id"], p["name"], p["name"].replace(" ", "")]:
            idx.setdefault(alias.lower(), p["id"])
        for alias in EXTRA_ALIASES.get(p["id"], []):
            idx.setdefault(alias.lower(), p["id"])
    return idx


ALIAS_INDEX = _build_alias_index()
ALIASES_SORTED = sorted(ALIAS_INDEX.keys(), key=len, reverse=True)

# ---------------------------------------------------------------- 词典
BRANDS = {
    "小米": ["小米", "红米", "redmi", "米家", "mi"],
    "苹果": ["苹果", "iphone", "ipone", "果子", "爱疯", "库克", "ios"],
    "华为": ["华为", "huawei", "菊花", "mate", "pura", "nova"],
    "荣耀": ["荣耀", "honor", "magic"],
    "OPPO": ["oppo", "欧珀", "findx"],
    "vivo": ["vivo", "维沃"],
    "一加": ["一加", "oneplus"],
    "三星": ["三星", "samsung", "galaxy"],
}

SCENES = {
    "拍照": ["拍照", "相机", "摄影", "影像", "拍人", "自拍", "出片", "长焦", "夜景", "视频", "拍视频", "拍娃", "拍演唱会", "演唱会"],
    "游戏": ["游戏", "电竞", "打游戏", "原神", "王者", "吃鸡", "帧率", "性能强", "性能党", "游戏党", "吃鸡"],
    "续航": ["续航", "电池", "电量", "耐用", "待机", "一天", "两天", "不用充电", "续航久"],
    "快充": ["快充", "充电快", "充得快", "百瓦", "闪充"],
    "小屏": ["小屏", "小巧", "单手", "小尺寸", "手小", "小一点"],
    "大屏": ["大屏", "屏幕大", "看剧", "追剧", "看视频", "大一点"],
    "轻薄": ["轻薄", "轻", "薄", "手感", "重量", "不重", "轻一点"],
    "性价比": ["性价比", "便宜", "划算", "实惠", "省钱", "价格低", "学生", "学生党", "预算少", "不贵", "亲民"],
    "商务": ["商务", "办公", "会议", "老板", "体面", "有面子", "高端"],
    "折叠": ["折叠", "小折叠", "大折叠", "折叠屏"],
    "系统": ["流畅", "系统", "生态", "ios", "苹果生态", "鸿蒙", "不卡", "卡顿", "稳定"],
    "自拍": ["自拍", "前置", "美颜"],
    "信号": ["信号", "通话", "地下车库", "电梯"],
    "办公": ["办公", "文档", "多任务"],
    "性能": ["性能", "顶配", "旗舰", "跑分", "处理器"],
    "音乐": ["音乐", "音质", "听歌"],
    "通勤": ["通勤", "上下班", "地铁"],
    "健康": ["健康", "心率", "血氧", "睡眠"],
    "居家": ["居家", "在家", "家里用", "宿舍"],
    "护肤": ["护肤", "保湿", "精华", "面霜"],
    "运动": ["运动", "跑步", "骑行", "健身"],
    "户外": ["户外", "露营", "徒步", "爬山"],
    "零食": ["零食", "吃的", "加餐", "坚果"],
}

# 商品库里的品牌自动并入品牌词典（多品类后品牌远多于手机）
for _brand in {p.get("brand") for p in PRODUCTS if p.get("brand")}:
    BRANDS.setdefault(_brand, [_brand.lower()])

SPEC_KEYS = [
    ("chip", "处理器"),
    ("screen", "屏幕"),
    ("battery", "电池"),
    ("charge", "充电"),
    ("camera", "影像"),
    ("weight", "重量"),
    ("storage", "存储"),
    ("system", "系统"),
]

# 品类词典：用户说这些词就把品类锁定
CATEGORIES = {
    "手机": ["手机", "手机壳", "旗舰机", "千元机"],
    "笔记本": ["笔记本", "电脑", "笔电", "macbook", "游戏本", "显示器"],
    "平板": ["平板", "pad", "ipad"],
    "耳机": ["耳机", "耳塞", "蓝牙耳机", "降噪耳", "音箱", "蓝牙音箱"],
    "智能穿戴": ["手表", "手环", "watch", "穿戴"],
    "家电": ["空调", "冰箱", "洗衣机", "扫地机", "扫地机器人", "豆浆机", "破壁机", "破壁",
             "吸尘器", "电饭煲", "空气炸锅", "咖啡机", "吹风机", "空气净化器", "加湿器",
             "微波炉", "烤箱", "洗碗机", "家电"],
    "美妆": ["美妆", "护肤", "精华", "口红", "蜜粉", "面霜", "化妆品", "面膜", "洗面奶",
             "粉底", "眼影", "香水", "防晒"],
    "食品": ["零食", "坚果", "牛奶", "大米", "饮料", "泡面", "食品", "咖啡豆", "面包", "饼干", "酸奶"],
    "服饰": ["衣服", "外套", "冲锋衣", "跑鞋", "鞋", "内衣", "穿搭", "羽绒服", "T恤", "裤子", "帽子"],
    "家居": ["家居", "沙发", "餐桌", "书桌", "香薰", "蜡烛", "家具", "床垫", "枕头", "台灯", "地毯", "收纳"],
}

# 生活场景细分：室内 / 室外
ENVIRONMENTS = {
    "indoor": ["室内", "家里", "家用", "居家", "办公", "卧室", "客厅", "厨房", "宿舍", "书房"],
    "outdoor": ["室外", "户外", "出门", "出行", "通勤", "旅行", "旅游", "露营", "运动", "跑步", "爬山", "骑行", "自驾"],
}

BRAND_TIER_LABELS = {"luxury": "高奢", "popular": "热门", "economy": "经济", "niche": "小众"}

# ---------------------------------------------------------------- 数字处理
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn2num(text):
    """把 '五千' '三千二' '一万二' 这类中文数字转成整数，失败返回 None。"""
    total, section, number, last_unit = 0, 0, 0, 0
    for ch in text:
        if ch in CN_DIGITS:
            number = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
            last_unit = unit
            number = 0
        else:
            return None
    if number:
        if last_unit and last_unit >= 100:
            section += number * (last_unit // 10)
        else:
            section += number
    return total + section


def normalize_numbers(text):
    """只转换带'十百千万'单位的中文数字，避免误伤'一加13'。"""

    def repl(m):
        token = m.group()
        if not any(ch in CN_UNITS for ch in token):
            return token
        value = cn2num(token)
        return str(value) if value else token

    return re.sub(r"[零〇一二两三四五六七八九十百千万]+", repl, text)


def fmt_sales(n):
    if n >= 10000:
        return f"{n / 10000:.1f}万".replace(".0万", "万")
    return str(n)


# ---------------------------------------------------------------- 方言归一化
def normalize_dialect(text, dialect_hint=None):
    """识别并归一化方言词，返回 (归一化文本, 命中方言信息或 None)。"""
    hits = []
    result = text
    for region in DIALECTS["regions"]:
        matched = [m for m in region["markers"] if m in text]
        if not matched:
            continue
        hits.append({"code": region["code"], "name": region["name"], "markers": matched[:3]})
        for phrase in sorted(region["phrases"].keys(), key=len, reverse=True):
            if phrase in result:
                result = result.replace(phrase, region["phrases"][phrase])

    if hits:
        return result, hits[0]
    if dialect_hint:
        for region in DIALECTS["regions"]:
            if region["code"] == dialect_hint:
                return result, {"code": region["code"], "name": region["name"], "markers": []}
    return result, None


def detect_product_mentions(text):
    """最长优先 + 从左到右扫描，命中即消费，避免"小米15 Pro"同时命中小米15。"""
    padded = text.lower().replace(" ", "")
    found = []
    i = 0
    while i < len(padded):
        hit = None
        for alias in ALIASES_SORTED:
            key = alias.replace(" ", "")
            if key and padded.startswith(key, i):
                hit = (key, ALIAS_INDEX[alias])
                break
        if hit:
            if hit[1] not in found:
                found.append(hit[1])
            i += len(hit[0])
        else:
            i += 1
    return found


# ---------------------------------------------------------------- 需求解析
def parse_budget(text):
    t = normalize_numbers(text)

    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK千万]\s*[-~—至到]\s*(\d+(?:\.\d+)?)\s*[kK千万]", t)
    if m:
        lo, hi = float(m.group(1)) * 1000, float(m.group(2)) * 1000
        return {"min": int(lo), "max": int(hi), "label": f"{int(lo)}-{int(hi)}元", "type": "range"}

    m = re.search(r"(\d{3,6})\s*(?:元|块)?\s*[-~—至到]\s*(\d{3,6})", t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return {"min": min(lo, hi), "max": max(lo, hi), "label": f"{min(lo, hi)}-{max(lo, hi)}元", "type": "range"}

    if "千元机" in t or "百元机" in t:
        return {"min": 0, "max": 2000, "label": "2000元以内", "type": "max"}

    m = re.search(r"预算(?:是|在|大概)?\s*(\d{3,6})", t)
    if m:
        v = int(m.group(1))
        return {"min": 0, "max": int(v * 1.15), "label": f"预算{v}元左右", "type": "max"}

    m = re.search(r"(\d{3,6})\s*(?:元|块)?\s*(?:以内|以下|之内|封顶)", t)
    if m:
        return {"min": 0, "max": int(m.group(1)), "label": f"{m.group(1)}元以内", "type": "max"}

    m = re.search(r"(?:不超过|不到|少于|最多|低于)\s*(\d{3,6})", t)
    if m:
        return {"min": 0, "max": int(m.group(1)), "label": f"{m.group(1)}元以内", "type": "max"}

    m = re.search(r"(\d{3,6})\s*(?:元|块)?\s*(?:左右|上下|附近|价位|档|的)", t)
    if m:
        v = int(m.group(1))
        return {"min": int(v * 0.85), "max": int(v * 1.15), "label": f"{v}元左右", "type": "around"}

    m = re.search(r"(\d{3,6})\s*(?:元|块)?\s*(?:以上|起步|往上)", t)
    if m:
        return {"min": int(m.group(1)), "max": 99999, "label": f"{m.group(1)}元以上", "type": "min"}

    m = re.search(r"(\d{3,6})\s*(?:元|块)", t)
    if m:
        v = int(m.group(1))
        return {"min": int(v * 0.85), "max": int(v * 1.15), "label": f"{v}元左右", "type": "around"}

    return None


def parse_scenes(text):
    found = []
    for scene, words in SCENES.items():
        for w in words:
            if w in text:
                if scene not in found:
                    found.append(scene)
                break
    return found


def parse_categories(text):
    low = text.lower()
    found = []
    for category, words in CATEGORIES.items():
        for word in words:
            if word.lower() in low:
                found.append(category)
                break
    return found


def parse_environments(text):
    low = text.lower()
    found = []
    for env, words in ENVIRONMENTS.items():
        for word in words:
            if word.lower() in low:
                found.append(env)
                break
    return found


def parse_brand_tiers(text):
    labels = {"luxury": ["高奢", "高端", "奢侈"], "popular": ["热门", "爆款", "畅销"],
              "economy": ["经济", "平价", "便宜", "省钱"], "niche": ["小众", "独特", "冷门"]}
    low = text.lower()
    found = []
    for tier, words in labels.items():
        for word in words:
            if word in low:
                found.append(tier)
                break
    return found


def parse_brands(text):
    found = []
    low = text.lower()
    for brand, words in BRANDS.items():
        for w in words:
            if w.lower() in low:
                if brand not in found:
                    found.append(brand)
                break
    return found


def extract_keywords(text):
    """抽出非功能词，用于自由文本匹配（先去掉价格表达和虚词/语气词）。"""
    cleaned = re.sub(r"[，。！？、,.!?：:\s]", " ", text)
    # 先清掉预算表达，避免“2000以内的扫地机器人”把数字并进关键词
    cleaned = re.sub(
        r"\d+(?:\.\d+)?\s*(?:k|K|千|万)?\s*(?:元|块)?\s*(?:以内|以下|之内|封顶|左右|上下|附近|以上|起步|价位|档|到)?\s*的?",
        " ", cleaned)
    stop = {
        "我要", "想买", "买个", "买一", "一部", "的", "款", "个", "机型", "请问",
        "我", "有", "没", "比较", "对比", "推荐", "一下", "啊", "呀", "吗", "呢", "吧",
        "呗", "哦", "嘛", "就", "都", "也", "还", "和", "跟", "比", "哪个", "哪款",
        "好", "选", "最", "看", "看看", "求", "帮", "查", "给我", "点", "些", "大概",
        "左右", "以内", "以上", "预算", "元", "块", "钱", "价格", "价位", "要求",
        "能", "可以", "不", "手机",
    }
    out = []
    for tk in re.split(r"\s+", cleaned):
        tk = tk.strip()
        if not tk or tk.isdigit() or tk in stop or len(tk) < 2:
            continue
        out.append(tk)
    return out[:6]


def _kw_variants(kw):
    """品类词去掉“机/器/表”后缀再匹配一次，兼容“破壁机”↔“破壁豆浆机”这类写法。"""
    out = [kw]
    if len(kw) >= 3 and kw[-1] in "机器表仪":
        out.append(kw[:-1])
    return out


COMPARE_RE = re.compile(r"对比|比较|哪个好|哪款好|哪个更|区别|差异|差在哪|选哪个|买哪个|还是|vs|VS|PK|pk")

EXCLUDE_RE = re.compile(r"(?:不要|别要|不想要|不想买|不喜欢|避开|排除|除开)\s*([\u4e00-\u9fa5A-Za-z0-9]{1,6})")


def parse_excludes(text):
    items = []
    for m in EXCLUDE_RE.finditer(text):
        value = m.group(1).strip()
        if value and value not in items:
            items.append(value)
    return items[:3]


def parse_query(raw_text, dialect_hint=None):
    normalized, dialect = normalize_dialect(raw_text, dialect_hint)
    text = normalized
    mentions = detect_product_mentions(text.lower().replace(" ", ""))
    budget = parse_budget(text)
    scenes = parse_scenes(text)
    brands = parse_brands(text)
    keywords = extract_keywords(text)

    has_compare = bool(COMPARE_RE.search(text))
    conj = ("和" in text or "跟" in text or "比" in text) and len(mentions) >= 2
    intent = "recommend"
    if has_compare or conj:
        intent = "compare"
    elif re.search(r"介绍|详情|详细|怎么样|参数|配置|值得买|好不好", text) and len(mentions) == 1:
        intent = "detail"
    elif re.search(r"我的|画像|喜好|偏好|记忆|了解我|记得", text) and not mentions:
        intent = "profile"
    elif re.search(r"你好|您好|hi|hello|在吗|开始|嗨", text, re.I) and not mentions and not budget:
        intent = "greet"
    elif not mentions and not budget and not scenes and not brands and len(text) < 4:
        intent = "unknown"

    if re.search(r"再来|还有|更多|换一批|其他", text):
        intent = "more"

    return {
        "raw": raw_text,
        "normalized": normalized,
        "dialect": dialect,
        "mentions": mentions,
        "budget": budget,
        "scenes": scenes,
        "brands": brands,
        "keywords": keywords,
        "exclude": parse_excludes(text),
        "categories": parse_categories(text),
        "environments": parse_environments(text),
        "brandTiers": parse_brand_tiers(text),
        "intent": intent,
    }


# ---------------------------------------------------------------- 记忆库
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_memory():
    return db.load_memory()


def _blank_profile():
    return {
        "createdAt": _now(),
        "updatedAt": _now(),
        "searches": [],
        "views": {},
        "clicks": {},
        "compares": [],
        "preferences": {"brand": {}, "scene": {}, "tag": {},
                        "environment": {}, "brandTier": {}, "category": {}},
        "budgetHistory": [],
        "avoidNew": True,
    }


def get_profile(user_id):
    with _lock:
        profile = _load_memory().get(user_id)
        if not profile:
            profile = _blank_profile()
            db.save_memory(user_id, profile)
        defaults = _blank_profile()
        for key, default in defaults.items():
            profile.setdefault(key, default)
        prefs = profile["preferences"]
        for key in defaults["preferences"]:
            prefs.setdefault(key, {})
        return profile


def _bump(mapping, key, weight=1, cap=30):
    if not key:
        return
    mapping[key] = min(cap, mapping.get(key, 0) + weight)


def record_search(user_id, parsed):
    with _lock:
        profile = get_profile(user_id)
        profile.setdefault("searches", [])
        profile["searches"].append({
            "text": parsed["raw"],
            "normalized": parsed["normalized"],
            "dialect": (parsed.get("dialect") or {}).get("name"),
            "intent": parsed["intent"],
            "budget": parsed.get("budget"),
            "scenes": parsed.get("scenes", []),
            "brands": parsed.get("brands", []),
            "categories": parsed.get("categories", []),
            "environments": parsed.get("environments", []),
            "ts": _now(),
        })
        profile["searches"] = profile["searches"][-60:]
        prefs = profile.setdefault("preferences", _blank_profile()["preferences"])
        for key in ("brand", "scene", "tag", "environment", "brandTier", "category"):
            prefs.setdefault(key, {})
        for b in parsed.get("brands", []):
            _bump(prefs["brand"], b, 1)
        for s in parsed.get("scenes", []):
            _bump(prefs["scene"], s, 1)
        for c in parsed.get("categories", []):
            _bump(prefs["category"], c, 1)
        for e in parsed.get("environments", []):
            _bump(prefs["environment"], e, 1)
        for t in parsed.get("brandTiers", []):
            _bump(prefs["brandTier"], t, 1)
        if parsed.get("budget"):
            profile.setdefault("budgetHistory", []).append({
                "min": parsed["budget"]["min"], "max": parsed["budget"]["max"], "ts": _now()
            })
            profile["budgetHistory"] = profile["budgetHistory"][-20:]
        profile["updatedAt"] = _now()
        db.save_memory(user_id, profile)
        return profile


def record_event(user_id, event_type, product_id):
    with _lock:
        profile = get_profile(user_id)
        product = product_by_id(product_id)
        if not product:
            return profile

        bucket_key = "views" if event_type == "view" else "clicks" if event_type == "click" else "compares"
        if bucket_key in ("views", "clicks"):
            bucket = profile.setdefault(bucket_key, {})
            item = bucket.setdefault(product_id, {"count": 0, "first": _now()})
            item["count"] += 1
            item["last"] = _now()
        else:
            profile.setdefault("compares", []).append({"ids": product_id, "ts": _now()})
            profile["compares"] = profile["compares"][-30:]

        prefs = profile.setdefault("preferences", _blank_profile()["preferences"])
        weight = 2 if event_type == "click" else 1
        _bump(prefs.setdefault("brand", {}), product["brand"], weight)
        for s in product.get("scenes", []):
            _bump(prefs.setdefault("scene", {}), s, weight)
        for t in product.get("tags", []):
            _bump(prefs.setdefault("tag", {}), t, weight)
        for env in product.get("environments", []):
            _bump(prefs.setdefault("environment", {}), env, weight)
        if product.get("brandTier"):
            _bump(prefs.setdefault("brandTier", {}), product["brandTier"], weight)
        if product.get("category"):
            _bump(prefs.setdefault("category", {}), product["category"], weight)

        profile["updatedAt"] = _now()
        db.save_memory(user_id, profile)
        return profile


def reset_memory(user_id):
    with _lock:
        db.delete_memory(user_id)
        return True


def set_setting(user_id, key, value):
    with _lock:
        profile = get_profile(user_id)
        profile[key] = value
        profile["updatedAt"] = _now()
        db.save_memory(user_id, profile)
        return profile


def _avg_budget(profile):
    hist = profile.get("budgetHistory") or []
    if not hist:
        return None
    lows = sorted(h["min"] for h in hist)
    highs = sorted(h["max"] for h in hist)
    mid = len(hist) // 2
    return {"min": lows[mid], "max": highs[mid]}


def profile_summary(profile):
    prefs = profile.get("preferences", {})
    brands = [k for k, v in sorted(prefs.get("brand", {}).items(), key=lambda kv: -kv[1])[:2] if v > 0]
    scenes = [k for k, v in sorted(prefs.get("scene", {}).items(), key=lambda kv: -kv[1])[:3] if v > 0]
    tags = [k for k, v in sorted(prefs.get("tag", {}).items(), key=lambda kv: -kv[1])[:3] if v > 0]
    categories = [k for k, v in sorted(prefs.get("category", {}).items(), key=lambda kv: -kv[1])[:3] if v > 0]
    viewed = set(profile.get("views", {}).keys())
    budget = _avg_budget(profile)

    env_raw = prefs.get("environment", {})
    env_total = sum(env_raw.values()) or 1
    environments = [
        {"key": key, "label": label, "score": round(env_raw.get(key, 0) / env_total * 100)}
        for key, label in (("indoor", "室内"), ("outdoor", "室外"))
    ]

    tier_raw = prefs.get("brandTier", {})
    tier_total = sum(tier_raw.values()) or 1
    brand_tiers = [
        {"key": key, "label": label, "score": round(tier_raw.get(key, 0) / tier_total * 100)}
        for key, label in BRAND_TIER_LABELS.items()
    ]

    parts = []
    if categories:
        parts.append("常看" + "、".join(categories))
    if scenes:
        parts.append("看重" + "、".join(scenes))
    if brands:
        parts.append("偏好" + "、".join(brands) + "品牌")
    top_env = max(environments, key=lambda item: item["score"])
    if top_env["score"] >= 50:
        parts.append(f"偏{top_env['label']}场景 {top_env['score']}%")
    top_tier = max(brand_tiers, key=lambda item: item["score"])
    if top_tier["score"] >= 40:
        parts.append(f"偏好{top_tier['label']}品牌 {top_tier['score']}%")
    if budget:
        parts.append(f"预算约 {budget['min']}-{budget['max']} 元")
    summary = "；".join(parts) if parts else "还没有形成明显偏好"

    return {
        "summary": summary,
        "brands": brands,
        "scenes": scenes,
        "tags": tags,
        "categories": categories,
        "environments": environments,
        "brandTiers": brand_tiers,
        "budget": budget,
        "viewedCount": len(viewed),
        "searchCount": len(profile.get("searches", [])),
        "recentSearches": [s["text"] for s in profile.get("searches", [])[-5:]][::-1],
        "avoidNew": profile.get("avoidNew", True),
        "untouched": len([p for p in _all_products() if p["id"] not in viewed]),
    }


# ---------------------------------------------------------------- 混合检索引擎
SYNONYMS = {
    "拍照": ["影像", "相机", "长焦", "夜景", "自拍"],
    "影像": ["拍照", "相机", "长焦"],
    "便宜": ["性价比", "千元", "实惠", "降价"],
    "性价比": ["便宜", "千元", "实惠"],
    "耐用": ["续航", "电池", "口碑"],
    "续航": ["电池", "快充", "耐用"],
    "游戏": ["性能", "散热", "高刷", "电竞"],
    "老人": ["长辈", "大字体", "续航"],
    "爸妈": ["长辈", "大字体", "续航"],
    "学生": ["性价比", "续航", "耐用"],
    "轻薄": ["轻巧", "小屏", "单手"],
    "小屏": ["单手", "轻巧", "轻薄"],
    "折叠": ["fold", "flip", "大屏"],
    "商务": ["办公", "信号", "旗舰"],
    "信号": ["商务", "通信", "卫星"],
    "自拍": ["前置", "美颜", "拍照"],
}

_INDEX = {"signature": None, "vectors": {}}


def _grams(text):
    cleaned = re.sub(r"[^\u4e00-\u9fa5a-z0-9]", "", (text or "").lower())
    return [cleaned[i:i + 2] for i in range(len(cleaned) - 1)]


def _doc_text(product):
    parts = [product["name"], product["brand"], product.get("category", ""),
             " ".join(product.get("tags", [])),
             " ".join(product.get("highlights", [])), product.get("audience", ""),
             " ".join(product.get("scenes", []))]
    parts += [str(v) for v in (product.get("specs") or {}).values()]
    return " ".join(parts)


def _build_index():
    """字符二元组 TF-IDF 向量，商品库变化时自动重建。"""
    items = _all_products()
    signature = tuple(p["id"] for p in items)
    if _INDEX["signature"] == signature:
        return
    docs = [_grams(_doc_text(p)) for p in items]
    df = {}
    for doc in docs:
        for g in set(doc):
            df[g] = df.get(g, 0) + 1
    total = max(len(docs), 1)
    idf = {g: math.log((total + 1) / (count + 1)) + 1 for g, count in df.items()}

    vectors = {}
    for product, doc in zip(items, docs):
        tf = {}
        for g in doc:
            tf[g] = tf.get(g, 0) + 1
        vec = {g: (1 + math.log(c)) * idf[g] for g, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors[product["id"]] = {g: v / norm for g, v in vec.items()}
    _INDEX.update(signature=signature, vectors=vectors)


def _query_vector(parsed):
    text = parsed.get("normalized", "") + " " + " ".join(parsed.get("keywords", []))
    for scene in parsed.get("scenes", []):
        text += " " + " ".join(SYNONYMS.get(scene, []))
    for kw in parsed.get("keywords", []):
        text += " " + " ".join(SYNONYMS.get(kw, []))
    tf = {}
    for g in _grams(text):
        tf[g] = tf.get(g, 0) + 1
    vec = {g: (1 + math.log(c)) for g, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {g: v / norm for g, v in vec.items()}


def semantic_score(parsed, product):
    _build_index()
    qv = parsed.get("_qvec")
    if qv is None:
        qv = _query_vector(parsed)
        parsed["_qvec"] = qv
    pv = _INDEX["vectors"].get(product["id"])
    if not pv or not qv:
        return 0.0
    return sum(qv.get(g, 0.0) * weight for g, weight in pv.items())


# ---------------------------------------------------------------- 大模型查询理解
UNDERSTAND_CACHE = {}

UNDERSTAND_SYSTEM = (
    "你是中文购物需求解析器。把用户一句话解析成 JSON，字段固定为："
    '{"budgetMin":整数或null,"budgetMax":整数或null,"brands":["小米"],'
    '"needs":["拍照","轻薄"],"exclude":["曲面屏"],"keywords":["小尺寸","给爸妈"]}。'
    "needs 只能取：拍照、游戏、续航、小屏、大屏、轻薄、性价比、折叠、商务、自拍、系统、信号。"
    "不确定的字段给空数组或 null；只输出 JSON，不要解释。"
)


def _chat_json(system, user, timeout=12):
    key, base, model = _llm_conf()
    if not key:
        return None
    import urllib.request

    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
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


def llm_understand(message):
    if not llm_enabled():
        return None
    message = (message or "").strip()
    if not message:
        return None
    hit = UNDERSTAND_CACHE.get(message)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    info = _chat_json(UNDERSTAND_SYSTEM, message)
    if isinstance(info, dict):
        UNDERSTAND_CACHE[message] = (time.time(), info)
        return info
    return None


def _enrich_parsed(parsed):
    """规则解析不够细时，用大模型补全预算/场景/否定条件/关键词。"""
    if parsed["intent"] not in ("recommend", "detail", "compare") or parsed.get("mentions"):
        return parsed
    if parsed.get("budget") and parsed.get("scenes"):
        return parsed
    info = llm_understand(parsed.get("raw", ""))
    if not info:
        return parsed

    if not parsed.get("budget"):
        lo = info.get("budgetMin") if isinstance(info.get("budgetMin"), int) else None
        hi = info.get("budgetMax") if isinstance(info.get("budgetMax"), int) else None
        if lo or hi:
            lo = lo or 0
            hi = hi or 99999
            label = f"{lo}-{hi} 元" if lo else f"{hi} 元以内"
            parsed["budget"] = {"min": lo, "max": hi, "label": label, "type": "llm"}

    for brand in info.get("brands") or []:
        if isinstance(brand, str) and brand and brand not in parsed["brands"]:
            parsed["brands"].append(brand)
    for need in info.get("needs") or []:
        if isinstance(need, str) and need in SCENES and need not in parsed["scenes"]:
            parsed["scenes"].append(need)
    for word in (info.get("needs") or []) + (info.get("keywords") or []):
        if isinstance(word, str) and len(word) >= 2 and word not in parsed["keywords"]:
            parsed["keywords"].append(word)
    for word in info.get("exclude") or []:
        if isinstance(word, str) and word and word not in parsed["exclude"]:
            parsed["exclude"].append(word)
    parsed["llmParsed"] = True
    return parsed


# ---------------------------------------------------------------- 联网商品补充
MODEL_RE = re.compile(
    r"(?:小米|红米|redmi|苹果|iphone|ipone|华为|荣耀|oppo|vivo|一加|三星|魅族|realme|iqoo|努比亚|moto)"
    r"\s*(?:mate|pura|nova|magic|find|turbo|flip|fold|k|note|x|数字)?\s*\d{1,3}\s*(?:pro|plus|max|ultra|mini|se|gt)?",
    re.I,
)
FRESH_RE = re.compile(r"最新|新款|新机|刚发布|刚出|今年|上市|2025|2026")

ALL_ALIASES = {a.lower() for values in EXTRA_ALIASES.values() for a in values}
ALL_ALIASES |= {p["name"].lower() for p in PRODUCTS}


def unknown_model(text):
    compact = re.sub(r"\s", "", (text or "").lower())
    for match in MODEL_RE.finditer(compact):
        phrase = match.group(0)
        if any(alias in phrase or phrase in alias for alias in ALL_ALIASES):
            continue
        return phrase
    return ""


def run_web_search(parsed):
    """只在“本地库没有的机型 / 问最新款”时联网，避免每次对话都外呼。"""
    if not websearch.enabled() or parsed["intent"] not in ("recommend", "detail", "compare"):
        return []
    compact = re.sub(r"\s", "", parsed.get("normalized", "").lower())
    model = unknown_model(compact)
    if model:
        query = f"{model} 价格 参数 评测"
    elif FRESH_RE.search(compact):
        query = f"{parsed.get('raw', '')} 价格 参数 最新"
    else:
        return []

    products = websearch.lookup(query)
    if products:
        parsed["webIds"] = [p["id"] for p in products]
        parsed["webQuery"] = query
    return products


# ---------------------------------------------------------------- 打分与推荐
def score_product(product, parsed, profile, feed_mode=False):
    raw = 0.0
    reasons = []

    rating = product.get("rating") or 92
    raw += (rating - 95) * 5.5

    for brand in parsed.get("brands", []):
        if product["brand"] == brand:
            raw += 26
            reasons.append(f"你点名要看{product['brand']}")

    matched_scenes = [s for s in parsed.get("scenes", []) if s in product["scenes"]]
    raw += len(matched_scenes) * 23
    if matched_scenes:
        reasons.append("符合你的" + "、".join(matched_scenes) + "需求")

    for kw in parsed.get("keywords", []):
        hay = (product["name"] + "".join(product["tags"]) + "".join(product["highlights"]) + product["audience"])
        if kw and kw in hay:
            raw += 6
        if any(v in product["name"] for v in _kw_variants(kw)):
            raw += 10

    budget = parsed.get("budget")
    if budget:
        if budget["min"] <= product["price"] <= budget["max"]:
            raw += 20
            reasons.append(f"落在 {budget['label']} 预算内")
        elif product["price"] > budget["max"]:
            over = (product["price"] / max(budget["max"], 1) - 1)
            raw -= min(55, over * 90)
            if over < 0.12:
                reasons.append("略超预算但与需求最接近")
        else:
            raw -= 8

    prefs = profile.get("preferences", {})
    mem_brand = prefs.get("brand", {}).get(product["brand"], 0)
    if mem_brand:
        raw += min(18, mem_brand * 3)
        if mem_brand >= 3:
            reasons.append(f"你历史上多次关注{product['brand']}")

    mem_scene = sum(prefs.get("scene", {}).get(s, 0) for s in product["scenes"])
    if mem_scene:
        raw += min(20, mem_scene * 2.2)
        top_scene = max(product["scenes"], key=lambda s: prefs.get("scene", {}).get(s, 0))
        if prefs.get("scene", {}).get(top_scene, 0) >= 2:
            reasons.append(f"记得你偏爱{top_scene}")

    mem_tag = sum(prefs.get("tag", {}).get(t, 0) for t in product["tags"])
    if mem_tag:
        raw += min(14, mem_tag * 1.8)

    if product.get("category") in parsed.get("categories", []):
        raw += 6
    envs = parsed.get("environments", [])
    if envs and any(env in product.get("environments", []) for env in envs):
        raw += 9
    mem_env = sum(prefs.get("environment", {}).get(env, 0) for env in product.get("environments", []))
    if mem_env:
        raw += min(16, mem_env * 2)
    tier = product.get("brandTier")
    if tier:
        mem_tier = prefs.get("brandTier", {}).get(tier, 0)
        if mem_tier:
            raw += min(12, mem_tier * 2)
            if mem_tier >= 3:
                reasons.append(f"你偏好{BRAND_TIER_LABELS.get(tier, tier)}品牌")

    hay = product["name"] + "".join(product["tags"]) + "".join(product["highlights"]) + product["audience"]
    for banned in parsed.get("exclude", []):
        if banned and banned in hay:
            raw -= 80

    raw += semantic_score(parsed, product) * 18

    if feed_mode and profile.get("avoidNew", True):
        viewed = profile.get("views", {})
        if viewed and product["id"] not in viewed:
            raw -= 18

    if not reasons:
        if product.get("rating"):
            reasons.append(f"好评率 {product['rating']}%，{fmt_sales(product['sales'])} 人好评")
        else:
            reasons.append("联网检索到的在售新机型，价格仅供参考")

    return raw, reasons[:3]


def _apply_filters(parsed, relax=False):
    budget = parsed.get("budget")
    brands = parsed.get("brands", [])
    result = _all_products()

    categories = parsed.get("categories", [])
    if categories:
        filtered = [p for p in result if p.get("category") in categories]
        if filtered:
            result = filtered

    tiers = parsed.get("brandTiers", [])
    if tiers:
        filtered = [p for p in result if p.get("brandTier") in tiers]
        if filtered:
            result = filtered

    envs = parsed.get("environments", [])
    if envs:
        filtered = [p for p in result if any(e in p.get("environments", []) for e in envs)]
        if filtered:
            result = filtered

    if brands:
        filtered = [p for p in result if p["brand"] in brands]
        if filtered:
            result = filtered

    # 具体品类词（如"扫地机器人""吹风机"）命中商品名时，先收窄到这些型号
    # 再叠加预算，避免预算把同类型商品全过滤后换成别的品类
    keywords = [k for k in parsed.get("keywords", []) if len(k) >= 3]
    if keywords:
        hits = [p for p in result
                if any(v in p["name"] for k in keywords for v in _kw_variants(k))]
        if hits:
            result = hits

    if budget and not relax:
        upper = budget["max"] * 1.28
        filtered = [p for p in result if p["price"] <= upper]
        if filtered:
            result = filtered

    excludes = parsed.get("exclude", [])
    if excludes:
        filtered = [p for p in result
                    if not any(e and e in p["name"] + "".join(p.get("tags", []) + p.get("highlights", [])) for e in excludes)]
        if filtered:
            result = filtered

    return result


def _spec_items(product):
    specs = product.get("specs")
    if isinstance(specs, dict) and specs:
        return [{"label": key, "value": value} for key, value in specs.items()]
    return [{"label": label, "value": product.get(key, "暂无")} for key, label in SPEC_KEYS]


# 商品图：按具体型号映射到对应品类图，联网商品按品类兜底
IMAGE_BY_ID = {
    "bose-ultra": "headphones",
    "shokz-openrun": "earbuds",
    "mi-band9": "smartwatch",
    "haier-fridge": "fridge",
    "dyson-hd15": "hairdryer",
    "roborock-s8": "robot_vacuum",
    "gree-ac": "air_conditioner",
    "midea-ac": "air_conditioner",
    "joyoung-soy": "blender",
    "sk2-facial": "serum",
    "lancome-serum": "serum",
    "proya-serum": "serum",
    "huaxizi-powder": "makeup",
    "perfect-diary": "makeup",
    "arcteryx-jacket": "jacket",
    "anta-shoes": "shoes",
    "uniqlo-fleece": "clothing",
    "neiwai-bra": "clothing",
    "linshi-sofa": "sofa",
    "yuanshi-table": "desk",
    "ikea-desk": "desk",
    "guanxia-candle": "candle",
    "squirrel-nuts": "nuts",
    "wuchang-rice": "rice",
    "yili-milk": "milk",
    "genki-soda": "soda",
    "baixiang-noodle": "noodles",
}

IMAGE_BY_CATEGORY = {
    "手机": "phone",
    "笔记本": "laptop",
    "平板": "tablet",
    "耳机": "earbuds",
    "智能穿戴": "smartwatch",
    "家电": "appliance",
    "美妆": "serum",
    "食品": "nuts",
    "服饰": "clothing",
    "家居": "sofa",
}


def product_image(product):
    slug = IMAGE_BY_ID.get(product["id"]) or IMAGE_BY_CATEGORY.get(product.get("category", ""))
    return f"img/products/{slug}.jpg" if slug else ""


def product_card(product, profile=None, match=0, reasons=None):
    rating = product.get("rating")
    sales = product.get("sales")
    return {
        "id": product["id"],
        "brand": product["brand"],
        "category": product.get("category", ""),
        "image": product_image(product),
        "name": product["name"],
        "price": product["price"],
        "rating": rating,
        "ratingText": f"{rating}%" if rating else "暂无",
        "sales": sales,
        "salesText": fmt_sales(sales) if sales else "暂无",
        "tags": product["tags"],
        "highlights": product["highlights"],
        "audience": product["audience"],
        "specs": _spec_items(product),
        "color": product.get("color", "#ff6900"),
        "color2": product.get("color2", "#ffb26b"),
        "match": match,
        "reasons": reasons or [],
        "web": product.get("origin") == "web",
        "note": product.get("note", ""),
        "sourceUrl": product.get("sourceUrl", ""),
    }


def recommend(parsed, profile, limit=3, feed_mode=False, exclude=None):
    candidates = _apply_filters(parsed)
    if not candidates:
        candidates = _apply_filters(parsed, relax=True)

    scored = []
    for p in candidates:
        if exclude and p["id"] in exclude:
            continue
        raw, reasons = score_product(p, parsed, profile, feed_mode=feed_mode)
        scored.append((raw, p, reasons))
    scored.sort(key=lambda x: -x[0])

    cards = []
    for raw, p, reasons in scored[:limit]:
        match = int(max(35, min(99, 50 + raw * 0.45)))
        cards.append(product_card(p, profile, match, reasons))
    return cards


def _lcs_match(query, text):
    """最长公共子串：把「扫地机器人」这类整句关键词对到商品名/卖点上。"""
    if not query or not text:
        return 0, ""
    a, b = query[:40], text[:220]
    best_len, best_end = 0, 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len, best_end = cur[j], i
        prev = cur
    if best_len < 2:
        return best_len, ""
    return best_len, a[best_end - best_len:best_end]


def _search_suggestions(limit=8):
    """空结果时给几个可直接点的品类词。"""
    seen = []
    for p in PRODUCTS:
        cat = p.get("category")
        if cat and cat not in seen:
            seen.append(cat)
    return seen[:limit]


def _merge_llm_hints(parsed, hints):
    """把前端大模型解析出的结构化线索并入规则解析结果（只补空缺、不覆盖）。"""
    if not isinstance(hints, dict):
        return parsed
    lo = hints.get("budgetMin")
    hi = hints.get("budgetMax")
    if not parsed.get("budget") and (isinstance(lo, int) or isinstance(hi, int)):
        lo = lo if isinstance(lo, int) else 0
        hi = hi if isinstance(hi, int) else 99999
        if lo or hi:
            label = f"{lo}-{hi} 元" if lo else f"{hi} 元以内"
            parsed["budget"] = {"min": lo, "max": hi, "label": label, "type": "llm"}
    for key in ("brands", "keywords", "exclude"):
        for word in hints.get(key) or []:
            if isinstance(word, str) and word and word not in parsed.get(key, []):
                parsed.setdefault(key, []).append(word)
    for need in hints.get("needs") or []:
        if isinstance(need, str) and need in SCENES and need not in parsed.get("scenes", []):
            parsed.setdefault("scenes", []).append(need)
    for cat in hints.get("categories") or []:
        if isinstance(cat, str) and cat in CATEGORIES and cat not in parsed.get("categories", []):
            parsed.setdefault("categories", []).append(cat)
    parsed["llmParsed"] = True
    return parsed


def search_cards(raw_query, user_id="anon", limit=10, hints=None):
    """站内检索式智能体：自然语言 → 结构化条件 → 排序卡片。

    与大模型解说层共用同一份解析结果：配置了 Key 时先用 DeepSeek 补全
    预算/场景/否定条件，再用同一套打分器排序，保证结果可解释、可复现。
    """
    query = (raw_query or "").strip()
    if not query:
        return {"query": "", "cards": [], "conditions": [], "relaxed": False,
                "suggestions": _search_suggestions(),
                "note": "输入商品名、品类或需求，例如「3000 以内拍照手机」"}

    profile = get_profile(user_id)
    parsed = parse_query(query)
    if isinstance(hints, dict):
        parsed = _merge_llm_hints(parsed, hints)
    else:
        try:
            parsed = _enrich_parsed(parsed)
        except Exception:  # noqa: BLE001  大模型不可用时静默回落规则解析
            pass

    candidates = _apply_filters(parsed)
    relaxed = False
    if not candidates:
        candidates = _apply_filters(parsed, relax=True)
        relaxed = True

    # 「不要小米」这类：排除词命中品牌时，既撤销它带来的品牌硬条件，又剔除该品牌商品
    all_brands = {p["brand"] for p in PRODUCTS}
    drop_brands = set()
    for word in parsed.get("exclude", []):
        if not word:
            continue
        for brand in all_brands:
            if word.startswith(brand) or brand.startswith(word):
                drop_brands.add(brand)
    if drop_brands:
        parsed["brands"] = [b for b in parsed.get("brands", []) if b not in drop_brands]
        candidates = _apply_filters(parsed)
        if not candidates:
            candidates = _apply_filters(parsed, relax=True)
            relaxed = True
        candidates = [p for p in candidates if p["brand"] not in drop_brands]

    q_clean = re.sub(r"[^0-9a-z\u4e00-\u9fa5]", "", (parsed.get("normalized") or "").lower())
    scored = []
    for p in candidates:
        raw, reasons = score_product(p, parsed, profile)
        hay = re.sub(r"[^0-9a-z\u4e00-\u9fa5]", "",
                     (p["name"] + "".join(p["tags"]) + "".join(p["highlights"])).lower())
        hit_len, hit_text = _lcs_match(q_clean, hay)
        if hit_len >= 2:
            raw += min(34, hit_len * 5)
            if hit_len >= 3 and hit_text:
                reasons.insert(0, f"命中「{hit_text}」")
        scored.append((raw, p, reasons[:3]))
    scored.sort(key=lambda x: -x[0])

    cards = [product_card(p, profile, int(max(35, min(99, 50 + raw * 0.45))), reasons)
             for raw, p, reasons in scored[:limit]]

    conditions = []
    budget = parsed.get("budget")
    if budget:
        label = budget.get("label") or f"{budget['min']}-{budget['max']} 元"
        conditions.append({"label": "预算", "value": label})
    for key, label in (("categories", "品类"), ("brands", "品牌"), ("scenes", "场景")):
        values = [v for v in parsed.get(key, []) if v]
        if values:
            conditions.append({"label": label, "value": "、".join(values[:4])})
    env_text = _env_labels(parsed.get("environments", []))
    if env_text:
        conditions.append({"label": "环境", "value": env_text})
    tiers = [BRAND_TIER_LABELS.get(t, t) for t in parsed.get("brandTiers", [])]
    if tiers:
        conditions.append({"label": "品牌档次", "value": "、".join(tiers)})
    keywords = [k for k in parsed.get("keywords", []) if k and len(k) <= 6]
    if keywords:
        conditions.append({"label": "关键词", "value": "、".join(keywords[:3])})
    excludes = [e for e in parsed.get("exclude", []) if e and len(e) <= 8]
    if excludes:
        conditions.append({"label": "排除", "value": "、".join(excludes)})
    for brand in sorted(drop_brands):
        conditions.append({"label": "排除品牌", "value": brand})

    try:
        record_search(user_id, parsed)
    except Exception:  # noqa: BLE001
        pass

    note = ""
    if parsed.get("llmParsed"):
        note = "DeepSeek 已补充理解你的需求"
    if relaxed:
        note = (note + "；" if note else "") + "严格条件没有命中，已放宽预算等硬条件，以下是相近选择"

    return {
        "query": query,
        "cards": cards,
        "conditions": conditions,
        "relaxed": relaxed,
        "llm": bool(parsed.get("llmParsed")),
        "note": note,
        "suggestions": _search_suggestions(),
        "total": len(candidates),
    }


def recommend_feed(user_id, limit=6):
    profile = get_profile(user_id)
    parsed = {"brands": [], "scenes": [], "keywords": [], "budget": None, "mentions": [], "intent": "feed"}
    prefs = profile.get("preferences", {})

    # 依据记忆画像构造软需求
    top_scenes = [k for k, _ in sorted(prefs.get("scene", {}).items(), key=lambda kv: -kv[1])[:2] if _ > 0]
    top_brands = [k for k, _ in sorted(prefs.get("brand", {}).items(), key=lambda kv: -kv[1])[:1] if _ > 0]
    parsed["scenes"] = top_scenes
    parsed["brands"] = []  # 推荐流不硬性限制品牌，只靠打分
    budget = _avg_budget(profile)
    if budget:
        parsed["budget"] = {"min": budget["min"], "max": budget["max"], "label": f"{budget['min']}-{budget['max']}元", "type": "range"}

    cards = recommend(parsed, profile, limit=limit, feed_mode=True)
    summary = profile_summary(profile)
    plan = build_purchase_plan(cards, parsed, profile, summary) if cards else None

    note = ""
    if summary["searchCount"] or summary["viewedCount"]:
        bits = []
        if top_scenes:
            bits.append("你偏爱" + "、".join(top_scenes))
        if top_brands:
            bits.append("常看" + "、".join(top_brands))
        if budget:
            bits.append(f"预算集中在 {budget['min']}-{budget['max']} 元")
        note = "，".join(bits) + "，已按这些偏好排序"
        if summary["avoidNew"] and summary["viewedCount"]:
            note += f"，并避开了 {summary['untouched']} 款你从没看过的商品"

    return {"products": cards, "profile": summary, "note": note, "plan": plan, "total": len(PRODUCTS)}


# ---------------------------------------------------------------- 对比表
def _best_index(values, mode):
    if mode == "min":
        return values.index(min(values))
    if mode == "max":
        return values.index(max(values))
    return None


def _spec_labels(items):
    labels = []
    for product in items:
        for item in _spec_items(product):
            if item["label"] not in labels:
                labels.append(item["label"])
    return labels


def build_compare(ids, profile=None, parsed=None):
    items = [p for p in (product_by_id(i) for i in ids) if p][:3]
    if not items:
        return None

    rows = [
        {"key": "price", "label": "价格", "group": "core", "best": "min",
         "values": [f"¥{p['price']}" for p in items],
         "raw": [p["price"] for p in items]},
        {"key": "rating", "label": "好评率", "group": "core", "best": "max",
         "values": [f"{p['rating']}%" if p.get("rating") else "暂无" for p in items],
         "raw": [p.get("rating") or 0 for p in items]},
        {"key": "sales", "label": "销量", "group": "core", "best": "max",
         "values": [f"{fmt_sales(p['sales'])}台" if p.get("sales") else "暂无" for p in items],
         "raw": [p.get("sales") or 0 for p in items]},
    ]
    for label in _spec_labels(items)[:7]:
        rows.append({
            "key": "spec:" + label, "label": label, "group": "spec", "best": None,
            "values": [dict((s["label"], s["value"]) for s in _spec_items(p)).get(label, "暂无") for p in items],
            "raw": [dict((s["label"], s["value"]) for s in _spec_items(p)).get(label, "暂无") for p in items],
        })

    best = {}
    for row in rows:
        if row["best"]:
            idx = _best_index(row["raw"], row["best"])
            if idx is not None:
                best[row["key"]] = idx

    advice = []
    cheapest = min(items, key=lambda p: p["price"])
    advice.append({"title": "价格最低", "text": f"{cheapest['name']}，¥{cheapest['price']}，比最高价省 ¥{max(p['price'] for p in items) - cheapest['price']}"})

    rated = [p for p in items if p.get("rating")]
    if rated:
        top_rated = max(rated, key=lambda p: p["rating"])
        advice.append({"title": "口碑最好", "text": f"{top_rated['name']}，好评率 {top_rated['rating']}%"})
    else:
        advice.append({"title": "口碑最好", "text": "联网商品暂无好评率数据，建议下单前看下商品页评价"})

    sellers = [p for p in items if p.get("sales")]
    if sellers:
        best_seller = max(sellers, key=lambda p: p["sales"])
        advice.append({"title": "销量最高", "text": f"{best_seller['name']}，累计 {fmt_sales(best_seller['sales'])}台"})

    if parsed and parsed.get("scenes"):
        scenes = parsed["scenes"]
        fit = max(items, key=lambda p: len([s for s in scenes if s in p.get("scenes", [])]) * 2 + (p.get("rating") or 90) / 100)
        advice.append({"title": f"最契合你的{'、'.join(scenes)}需求", "text": f"{fit['name']}：{fit['highlights'][0]}"})

    return {
        "products": [product_card(p, profile) for p in items],
        "rows": rows,
        "best": best,
        "advice": advice,
        "ids": [p["id"] for p in items],
    }


# ---------------------------------------------------------------- 可解释购买方案
CATEGORY_RISK = {
    "家电": "大家电涉及配送安装，确认是否含上门安装、旧机回收，并保留发票",
    "美妆": "美妆开封后通常不支持退换，认准官方渠道和防伪标识",
    "食品": "食品注意保质期与是否临期，优先官方旗舰店",
    "服饰": "服饰注意尺码表和七天无理由退换",
    "手机": "手机确认国行版本、保修年限，优先支持官方售后的渠道",
    "笔记本": "笔记本确认保修年限与是否支持上门/寄修",
    "平板": "平板确认保修与是否含笔/键盘配件",
    "耳机": "耳机注意是否支持试用退换与保修时长",
    "智能穿戴": "穿戴设备注意表带尺寸与保修范围",
    "家居": "家居注意送货安装、尺寸是否进得了门",
}


def _env_labels(keys):
    labels = {"indoor": "室内", "outdoor": "室外"}
    return "、".join(labels.get(k, k) for k in keys or [])


def _after_sales_risk(product):
    """按品牌档位、来源、品类、好评率给出售后风险与规避建议。"""
    level = "低"
    items = []
    if product.get("origin") == "web":
        level = "中"
        items.append("联网检索款，下单前确认店铺是否官方授权、是否支持全国联保")
    if product.get("brandTier") == "niche":
        level = "中"
        items.append("小众品牌服务网点少，维修和配件等待时间可能更长")
    if product.get("brandTier") == "luxury":
        items.append("客单价偏高，建议加购延保/意外险并保留发票")
    note = CATEGORY_RISK.get(product.get("category", ""))
    if product.get("category") == "家电" and any(
            k in product.get("name", "") for k in ("吹风", "破壁", "豆浆", "电饭煲", "空气炸", "咖啡", "加湿", "风扇")):
        note = "小家电注意保修时长与是否支持只换不修，优先官方旗舰店"
    if note:
        items.append(note)
    rating = product.get("rating") or 0
    if rating and rating < 92:
        level = "中"
        items.append(f"好评率 {rating}%，建议先翻差评确认共性问题")
    if (product.get("price") or 0) >= 8000:
        items.append("单价较高，优先选支持价保和七天无理由的渠道")
    return {"level": level, "items": items[:3]}


def _fit_reasons(card, raw, parsed, summary):
    """把预算、场景、习惯、口碑组合成“为什么适合你”。"""
    head = []
    budget = parsed.get("budget")
    if budget and budget.get("max"):
        if budget["min"] <= raw["price"] <= budget["max"]:
            left = budget["max"] - raw["price"]
            txt = f"¥{raw['price']} 落在你 {budget['label']} 的预算内"
            if budget.get("type") != "min" and left >= 100:
                txt += f"，还留 ¥{left} 余量给配件/延保"
            elif 0 < left < 100:
                txt += "，几乎用满预算"
            head.append(txt)
        elif raw["price"] < budget["min"]:
            head.append(f"¥{raw['price']} 低于你 {budget['label']} 的预算下限，预算比较宽裕")
    envs = parsed.get("environments") or []
    hit_env = [e for e in envs if e in (raw.get("environments") or [])]
    if hit_env:
        head.append(f"覆盖你说的{_env_labels(hit_env)}使用场景")
    scenes = parsed.get("scenes") or []
    hit_scene = [s for s in scenes if s in (raw.get("scenes") or []) or s in (raw.get("tags") or [])]
    if hit_scene:
        head.append("符合你看重的" + "、".join(hit_scene[:2]))
    top_env = max(summary["environments"], key=lambda x: x["score"])
    if top_env["score"] >= 50 and top_env["key"] in (raw.get("environments") or []) and not hit_env:
        head.append(f"你 {top_env['score']}% 的浏览偏{top_env['label']}，这款也适配")
    top_tier = max(summary["brandTiers"], key=lambda x: x["score"])
    if top_tier["score"] >= 40 and raw.get("brandTier") == top_tier["key"]:
        head.append(f"是你偏好的{top_tier['label']}品牌档位")
    if raw.get("rating") and raw.get("sales"):
        head.append(f"好评率 {raw['rating']}%、销量 {fmt_sales(raw['sales'])}，口碑经过市场验证")
    elif raw.get("origin") == "web":
        head.append("本地库没有同款，这条是联网查到的在售信息")

    # 打分理由里常含相同关键词（如“通勤”“好评”），避免与上面的理由重复
    overlap = set(hit_scene) | set(hit_env) | set(scenes) | set(envs)
    head_text = "".join(head)
    tail = []
    for r in card.get("reasons") or []:
        if not r or any(k in r for k in overlap):
            continue
        if "好评" in r and "好评" in head_text:
            continue
        if "销量" in r and "销量" in head_text:
            continue
        if "预算" in r and "预算" in head_text:
            continue
        tail.append(r)

    seen, out = set(), []
    for r in head + tail:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out[:4]


def _plan_alternatives(cards, primary):
    others = [c for c in cards if c["id"] != primary["id"]]
    if not others:
        return []
    picks = []
    cheaper = [c for c in others if c["price"] < primary["price"]]
    if cheaper:
        pick = max(cheaper, key=lambda c: c["price"])
        picks.append((pick, f"比主推省 ¥{primary['price'] - pick['price']}"))
    rated = [c for c in others if c.get("rating")]
    if rated:
        best = max(rated, key=lambda c: c["rating"])
        if best["id"] not in [p[0]["id"] for p in picks]:
            picks.append((best, f"口碑更稳，好评 {best['rating']}%"))
    sold = [c for c in others if c.get("sales")]
    if sold:
        top = max(sold, key=lambda c: c["sales"])
        if top["id"] not in [p[0]["id"] for p in picks]:
            picks.append((top, f"买的人更多，销量 {top['salesText']}"))
    if not picks:
        picks.append((others[0], "同价位备选，可以一起对比"))
    return [
        {"id": c["id"], "name": c["name"], "price": c["price"], "role": role, "image": c.get("image", "")}
        for c, role in picks[:2]
    ]


def build_purchase_plan(cards, parsed, profile, summary=None):
    """生成可解释购买方案：预算、场景、口碑、售后风险、推荐理由与备选。"""
    if not cards:
        return None
    summary = summary or profile_summary(profile)
    primary = cards[0]
    raw = product_by_id(primary["id"]) or {}
    budget = parsed.get("budget") or summary.get("budget")
    alt = _plan_alternatives(cards, primary)

    if budget and budget.get("max"):
        inside = [c for c in cards if budget["min"] <= c["price"] <= budget["max"]]
        below = [c for c in cards if c["price"] < budget["min"]]
        over = [c for c in cards if c["price"] > budget["max"]]
        if inside:
            text = f"预算 {budget['label']}：{len(cards)} 款里 {len(inside)} 款在预算内"
        elif below and not over:
            text = f"预算 {budget['label']}：这几款都低于预算下限，预算比较宽裕"
        elif over and not below:
            text = f"预算 {budget['label']}：这几款略超上限"
        else:
            text = f"预算 {budget['label']}：有低于下限的，也有略超上限的"
        if over:
            text += f"；略超的是 {over[0]['name']} ¥{over[0]['price']}"
        if primary["price"] < budget["min"]:
            text += f"；主推 ¥{primary['price']}，比下限还低 ¥{budget['min'] - primary['price']}"
        elif primary["price"] > budget["max"]:
            text += f"；主推超预算 ¥{primary['price'] - budget['max']}"
            text += "，可以先看备选" if alt else "，同类型里没有更便宜的，建议放宽预算或等促销"
        elif budget.get("type") != "min":
            left = budget["max"] - primary["price"]
            text += f"；主推还剩 ¥{left} 余量" if left >= 100 else f"；主推几乎用满预算（仅余 ¥{left}）"
    else:
        text = "你还没给预算，我先按匹配度和口碑排序；补一句预算可以更精准"

    envs = parsed.get("environments") or [e["key"] for e in summary["environments"] if e["score"] >= 50]
    scenes = parsed.get("scenes") or summary.get("scenes", [])
    scenario_bits = []
    if envs:
        scenario_bits.append(f"{_env_labels(envs)}使用")
    if scenes:
        scenario_bits.append("看重" + "、".join(scenes[:3]))
    if parsed.get("categories"):
        scenario_bits.append("品类锁定" + "、".join(parsed["categories"]))
    scenario_text = "，".join(scenario_bits) if scenario_bits else "按你历史偏好推断"

    why = _fit_reasons(primary, raw, parsed, summary)
    risk = _after_sales_risk(raw)

    review_text = (
        f"好评率 {primary['ratingText']}，销量 {primary['salesText']}"
        if primary.get("rating")
        else "联网款暂无评价样本，建议看商品页近 30 天评价与差评关键词"
    )

    verdict = f"如果只买一款，我建议 {primary['name']}"
    if alt:
        verdict += f"；备选 {alt[0]['name']}（{alt[0]['role']}）"
        if len(alt) > 1:
            verdict += f"，也可看 {alt[1]['name']}（{alt[1]['role']}）"
    verdict += "。"

    return {
        "title": ("、".join(parsed.get("categories") or []) or (raw.get("category") or "商品")) + "购买方案",
        "budget": {"label": budget["label"] if budget else "未设置", "text": text},
        "scenario": {"text": scenario_text},
        "primary": {
            "id": primary["id"], "name": primary["name"], "price": primary["price"],
            "ratingText": primary["ratingText"], "salesText": primary["salesText"],
            "image": primary.get("image", ""), "why": why,
        },
        "alternatives": alt,
        "reviews": {"text": review_text, "web": primary.get("web", False)},
        "risk": risk,
        "verdict": verdict,
        "steps": [
            "优先官方旗舰店/自营，确认发票、保修与是否国行",
            "下单前比价并确认价保规则，避免刚买就降价",
            "收货 7 天内集中试用，保留包装以便退换",
        ],
    }


# ---------------------------------------------------------------- 话术生成
def _fmt_price(p):
    return f"¥{p['price']}"


def _card_sentence(card):
    return f"{card['name']}（{_fmt_price(card)}，好评 {card['ratingText']}，销量 {card['salesText']}）"


def build_reply(parsed, cards, compare_data, profile, extra=None):
    dialect = parsed.get("dialect")
    prefix = f"（听出你说的是{dialect['name']}，已经自动转成普通话理解啦）" if dialect else ""
    summary = profile_summary(profile)
    intent = parsed["intent"]
    extra = extra or {}

    if intent == "greet" or (intent == "unknown" and not cards):
        return (
            f"{prefix}你好，我是小函，你的智能导购助手。手机、电脑、耳机、家电、美妆、食品都能挑，"
            "告诉我想买什么、预算多少、室内还是室外用，我就能帮你选；"
            "你也可以直接说“小米 15 和 iPhone 16 哪个好”，我会给你一张价格/好评/销量的对比表。"
            f"（{summary['summary']}）"
        ), ["想买拍照好的手机", "通勤用的降噪耳机", "家里用的扫地机器人"]

    if intent == "profile":
        recent = "、".join(summary["recentSearches"]) if summary["recentSearches"] else "暂无"
        text = (
            f"{prefix}我对你的记忆是这样的：{summary['summary']}。"
            f"你一共搜索过 {summary['searchCount']} 次，看过 {summary['viewedCount']} 款商品，最近在看：{recent}。"
            + ("当前我还会避开你从没看过的商品。" if summary["avoidNew"] else "当前我允许推荐你没看过的新商品。")
        )
        return text, ["给我推荐几款", "帮我对比一下", "清空我的记忆"]

    if intent == "compare":
        if not compare_data:
            return (
                f"{prefix}我还没听清你要比哪两款，可以说“小米 15 和 iPhone 16 哪个好”，"
                "或者直接告诉我两个品牌，比如“小米和苹果对比一下”。"
            ), ["小米 15 对比 iPhone 16", "小米和苹果哪个好", "对比前两款"]
        names = [p["name"] for p in compare_data["products"]]
        cheapest = compare_data["advice"][0]["text"]
        rated = compare_data["advice"][1]["text"]
        sold = compare_data["advice"][2]["text"]
        verdict = compare_data["advice"][3] if len(compare_data["advice"]) > 3 else None
        text = (
            f"{prefix}我把 {' / '.join(names)} 放在一起比了，价格、好评率、销量做成表格放在右侧。\n"
            f"· 价格：{cheapest}\n"
            f"· 好评：{rated}\n"
            f"· 销量：{sold}\n"
        )
        if verdict:
            text += f"· 贴合你的需求：{verdict['text']}\n"
        text += "结论我写在表格下方，想看哪款的细节也可以直接问我。"
        text += "\n\n是否需要为您提供一份表格？"
        return text, ["需要，给我一份表格", "再看详细参数", "换成小米和苹果对比", "直接买哪款更稳"]

    if intent == "detail" and cards:
        card = cards[0]
        p = product_by_id(card["id"])
        lines = [
            f"{prefix}「{p['name']}」目前 {_fmt_price(p)}，好评率 {card['ratingText']}，销量 {card['salesText']}。",
            "核心参数：" + " · ".join(f"{s['label']} {s['value']}" for s in card["specs"]),
            "亮点：" + "；".join(p["highlights"]),
            f"适合：{p['audience']}。",
        ]
        if p.get("origin") == "web":
            lines.append(f"说明：这款是联网检索到的在售机型，{p.get('note', '价格仅供参考')}。")
        return "\n".join(lines), ["对比同价位机型", f"对比{p['name']}和 iPhone 16", "还有更便宜的吗"]

    plan = extra.get("plan")
    if cards and plan:
        primary = plan["primary"]
        lines = [
            f"{prefix}我按「{_describe_needs(parsed)}」筛了一遍，给你一份能直接执行的购买方案：",
            "",
            f"主推：{primary['name']}（¥{primary['price']}，好评 {primary['ratingText']}，销量 {primary['salesText']}）",
            "为什么适合你：",
        ]
        lines += [f"· {r}" for r in primary["why"]]
        lines.append(f"评价：{plan['reviews']['text']}")
        risk_items = "；".join(plan["risk"]["items"]) or "常规售后，按品类保修政策执行"
        lines.append(f"售后风险（{plan['risk']['level']}）：{risk_items}")
        if plan["alternatives"]:
            alt_text = "；".join(f"{a['name']} ¥{a['price']}（{a['role']}）" for a in plan["alternatives"])
            lines.append(f"备选：{alt_text}")
        lines.append(f"结论：{plan['verdict']}")
        lines.append("下单前三步：" + " ".join(f"{i}. {s}" for i, s in enumerate(plan["steps"], 1)))
        if extra.get("relaxed"):
            lines.append("严格符合条件的选择不多，我稍微放宽了预算范围，这几款最接近。")
        web_cards = [c for c in cards if c.get("web")]
        if web_cards:
            lines.append(f"其中 {'、'.join(c['name'] for c in web_cards)} 是联网检索到的参考价，实际以下单页面为准。")
        quicks = [f"介绍一下{primary['name']}", "对比这 %d 款" % len(cards), "再便宜一点"]
        return "\n".join(lines), quicks

    if cards:
        lines = [f"{prefix}我按「{_describe_needs(parsed)}」筛了一遍，这 {len(cards)} 款最贴："]
        for idx, card in enumerate(cards, 1):
            line = f"{idx}. {_card_sentence(card)}"
            if card["reasons"]:
                line += f" —— {card['reasons'][0]}"
            lines.append(line)
        if summary["searchCount"] or summary["viewedCount"]:
            note_bits = []
            if summary["scenes"]:
                note_bits.append("你偏爱" + "、".join(summary["scenes"]))
            if summary["brands"]:
                note_bits.append("常看" + "、".join(summary["brands"]))
            if note_bits:
                lines.append("顺带说一句，" + "、".join(note_bits) + "，所以我把这类商品的权重调高了。")
        if extra.get("relaxed"):
            lines.append("严格符合条件的选择不多，我稍微放宽了预算范围，这几款最接近。")
        web_cards = [c for c in cards if c.get("web")]
        if web_cards:
            lines.append(f"其中 {'、'.join(c['name'] for c in web_cards)} 是联网检索到的参考价，实际以下单页面为准。")
        lines.append("要不要我把这几款做成对比表？")
        quicks = ["对比这 %d 款" % len(cards), "再便宜一点", "只看拍照强的"]
        return "\n".join(lines), quicks

    return (
        f"{prefix}没找到完全符合的商品。换个说法试试：告诉我品类（手机 / 笔记本 / 耳机 / 家电 / 美妆…）、"
        "预算（比如 3000-4000 元）、使用场景（室内 / 室外）或最看重的点（拍照 / 游戏 / 降噪 / 轻便）。"
    ), ["3000-4000 元拍照好的手机", "通勤用的降噪耳机", "家里用的扫地机器人"]


def _describe_needs(parsed):
    parts = []
    if parsed.get("categories"):
        parts.append("、".join(parsed["categories"]))
    if parsed.get("budget"):
        parts.append(parsed["budget"]["label"])
    if parsed.get("brands"):
        parts.append("、".join(parsed["brands"]))
    if parsed.get("scenes"):
        parts.append("看重" + "、".join(parsed["scenes"]))
    if parsed.get("environments"):
        labels = {"indoor": "室内", "outdoor": "室外"}
        parts.append("、".join(labels.get(e, e) for e in parsed["environments"]) + "用")
    if parsed.get("mentions"):
        names = "、".join(product_by_id(i)["name"] for i in parsed["mentions"])
        parts.append(f"提到{names}")
    elif parsed.get("webIds"):
        names = "、".join(product_by_id(i)["name"] for i in parsed["webIds"][:2])
        parts.append(f"提到{names}")
    return " / ".join(parts) if parts else "你的需求"


# ---------------------------------------------------------------- 对比表格导出
EXPORT_DIR = os.path.join(os.environ.get("APP_WRITE_DIR") or BASE_DIR, "exports")
EXPORT_TTL = 1800  # 30 分钟内可继续导出上一次对比
_compare_cache = {}

NEGATIVE_WORDS = ("不需要", "不用", "不要", "别", "算了", "暂时不", "先不")
AFFIRM_WORDS = ("需要", "要", "好的", "好", "可以", "行", "是的", "是", "嗯", "对",
                "没问题", "来一份", "给我一份", "要一份", "生成", "导出", "下载", "提供", "麻烦")


def _remember_compare(user_id, model, ids=None):
    """记住最近一次对比，用户说“需要表格”时据此生成文件。"""
    _compare_cache[user_id] = {"model": model, "ids": ids or [], "ts": time.time()}
    if len(_compare_cache) > 200:
        for key in sorted(_compare_cache, key=lambda k: _compare_cache[k]["ts"])[:50]:
            _compare_cache.pop(key, None)


def _is_export_request(text):
    """判断用户这句是不是“要表格/要下载”。"""
    norm = re.sub(r"[\s，。！？、,.!?~～:：;；\"'“”‘’]+", "", text or "")
    if not norm or len(norm) > 20:
        return False
    if any(w in norm for w in NEGATIVE_WORDS):
        return False
    if "表格" in norm:
        return any(w in norm for w in ("需要", "要", "给我", "来", "下载", "导出", "生成", "提供", "发"))
    return any(norm == w or norm in (w + "的", w + "吧", w + "啊", w + "呀") for w in AFFIRM_WORDS)


def _compare_export_model(compare_data):
    products = compare_data.get("products") or []
    headers = ["对比维度"] + [p["name"] for p in products]
    rows = [[r["label"]] + list(r["values"]) for r in compare_data.get("rows", [])]
    notes = [f"{a['title']}：{a['text']}" for a in compare_data.get("advice", [])]
    return {
        "title": "商品对比表",
        "headers": headers,
        "rows": rows,
        "notes": notes,
        "file_names": [p["name"] for p in products],
    }


def _export_reply(user_id):
    cached = _compare_cache.get(user_id)
    if not cached:
        return None
    if time.time() - cached["ts"] > EXPORT_TTL:
        _compare_cache.pop(user_id, None)
        return None

    model = cached["model"]
    filename = xlsx_export.new_filename()
    path = os.path.join(EXPORT_DIR, filename)
    try:
        xlsx_export.write_xlsx(model, path)
    except Exception:
        return None

    names = model.get("file_names") or [n for n in (model.get("headers") or [])[1:] if n]
    names = [re.sub(r"（原商品）$", "", n).strip() for n in names if n]
    display = xlsx_export.safe_filename(
        "商品对比_" + "_".join(names[:3]) + ".xlsx" if names else "商品对比表.xlsx"
    )
    url = f"/download/{filename}?name={urllib.parse.quote(display, safe='')}"
    reply = "\n".join([
        "**表格已生成**",
        "",
        f"- 文件：{display}（Excel .xlsx，可直接用 Microsoft Excel / WPS 打开）",
        f"- 内容：{model.get('title', '商品对比表')}，共 {len(model.get('rows', []))} 项对比，附选购结论",
        f"- 下载链接：[点击下载 {display}]({url})",
        "",
        "> 点击链接会把文件下载到你的电脑，双击即可在本地用 Excel / WPS 打开。",
    ])
    return {
        "reply": reply,
        "markdown": True,
        "download": {"url": url, "name": display, "kind": "Excel"},
        "narrator": "template",
        "intent": "export",
        "dialect": None,
        "parsed": {"normalized": "", "budget": None, "brands": [], "scenes": [], "mentions": []},
        "products": [],
        "compare": None,
        "plan": None,
        "quickReplies": ["再看看对比表", "继续帮我挑一款"],
        "profile": profile_summary(get_profile(user_id)),
    }


# ---------------------------------------------------------------- 链接导购
LINK_HINTS = [
    ("手机", ["手机", "iphone", "ipone", "redmi", "mate", "pura", "nova", "galaxy", "5g", "折叠屏"]),
    ("笔记本", ["笔记本", "笔电", "macbook", "游戏本", "轻薄本", "酷睿", "锐龙", "rtx", "thinkpad"]),
    ("平板", ["平板", "ipad", "matepad"]),
    ("耳机", ["耳机", "airpods", "降噪耳", "蓝牙耳机"]),
    ("智能穿戴", ["手表", "手环", "watch", "穿戴"]),
    ("家电", ["空调", "冰箱", "洗衣机", "扫地机", "扫地机器人", "吹风机", "豆浆机", "破壁机", "净化器"]),
    ("美妆", ["精华", "口红", "蜜粉", "面霜", "护肤", "神仙水", "粉底"]),
    ("食品", ["坚果", "牛奶", "大米", "泡面", "饮料", "零食"]),
    ("服饰", ["外套", "冲锋衣", "跑鞋", "鞋", "内衣", "羽绒服"]),
    ("家居", ["沙发", "餐桌", "书桌", "香薰", "蜡烛", "床垫"]),
]


def _norm_key(text):
    return re.sub(r"[\s/·\-_()（）\[\]【】,，.。:：|]+", "", (text or "").lower())


def _match_link_product(title):
    """把链接标题匹配到站内同款，用于补全品牌、参数与参考价。"""
    norm = _norm_key(title)
    if len(norm) < 3:
        return None
    best, best_len = None, 0
    for p in _all_products():
        names = [_norm_key(p.get("name"))] + [_norm_key(a) for a in EXTRA_ALIASES.get(p["id"], [])]
        for cand in names:
            if len(cand) >= 3 and cand in norm and len(cand) > best_len:
                best, best_len = p, len(cand)
    return best


def _link_category(title, linked):
    if linked and linked.get("category"):
        return linked["category"]
    cats = parse_categories(title)
    if cats:
        return cats[0]
    low = (title or "").lower()
    for category, words in LINK_HINTS:
        if any(w in low for w in words):
            return category
    return ""


def _title_specs(title):
    """标题里能直接读到的硬参数，读不到就交给调用方标注【暂无公开参数】。"""
    bits = []
    m = re.search(r"(\d+(?:\.\d+)?\s*(?:GB|TB|G|T))(?![a-z])", title or "", re.I)
    if m:
        bits.append(m.group(1).upper().replace(" ", ""))
    m = re.search(r"(\d+(?:\.\d+)?\s*英寸)", title or "")
    if m:
        bits.append(m.group(1).replace(" ", ""))
    if "5g" in (title or "").lower():
        bits.append("5G")
    return " / ".join(bits)


def _info_specs_text(info):
    """链接解析阶段从搜索参数页补全的参数。"""
    specs = (info or {}).get("specs") or {}
    return "；".join(f"{k} {v}" for k, v in list(specs.items())[:6])


def _pick_competitors(linked, category, anchor_price, limit=5):
    pool = [p for p in _all_products() if p.get("category") == category]
    if linked:
        pool = [p for p in pool if p["id"] != linked["id"]]
    if not pool:
        return []

    if anchor_price:
        near = [p for p in pool
                if abs(p["price"] - anchor_price) <= max(anchor_price * 0.35, 300)]
        if len(near) < 3:
            near = sorted(pool, key=lambda p: abs(p["price"] - anchor_price))
        near.sort(key=lambda p: (abs(p["price"] - anchor_price), -(p.get("rating") or 0)))
    else:
        near = sorted(pool, key=lambda p: (-(p.get("rating") or 0), -(p.get("sales") or 0), p["price"]))
    return near[:limit]


def _md_cell(value):
    text = str(value if value is not None else "")
    return re.sub(r"\s+", " ", text).replace("|", "／").strip() or "【暂无公开参数】"


def _price_cell(product, link_price=None, original=False):
    if original:
        if link_price:
            return f"¥{link_price:g}"
        ref = (product or {}).get("price")
        return "【暂无公开参数】" + (f"（站内参考 ¥{ref:g}）" if ref else "")
    return f"¥{product['price']:g}" if product.get("price") else "【暂无公开参数】"


def _short_specs(product):
    if not product:
        return ""
    items = _spec_items(product)[:3]
    return " / ".join(f"{i['label']} {i['value']}" for i in items if i.get("value"))


def _weak_points(product, table_products):
    if not product:
        return "【暂无公开参数】"
    pid = product.get("id")
    others = [p for p in table_products if p is not product and p.get("id") != pid]
    facts = []
    rating = product.get("rating")
    price = product.get("price")
    sales = product.get("sales")
    other_ratings = [p.get("rating") for p in others if p.get("rating")]
    other_prices = [p.get("price") for p in others if p.get("price")]
    other_sales = [p.get("sales") for p in others if p.get("sales")]

    if rating and other_ratings and rating < min(other_ratings) and min(other_ratings) - rating >= 0.5:
        facts.append(f"好评率 {rating}%，低于表中其他款")
    if price and other_prices and price > max(other_prices):
        facts.append("价格表中最高")
    if sales and other_sales and sales < min(other_sales) * 0.6:
        facts.append("销量相对偏低")
    if product.get("note"):
        facts.append(product["note"])
    return "；".join(facts) if facts else "【暂无公开参数】"


def _link_verdict(linked, link_price, competitors):
    anchor = link_price or (linked or {}).get("price")
    lines = []

    if not competitors:
        lines.append("- 结论：暂未在站内找到同类型同价位的可比机型，先不下判断。")
        lines.append("- 建议：确认具体型号（如容量 / 版本）后再来对比，避免买错版本。")
        return lines

    if anchor:
        best = max(competitors, key=lambda p: (p.get("rating") or 0) - abs(p["price"] - anchor) / anchor * 12)
    else:
        best = max(competitors, key=lambda p: ((p.get("rating") or 0), p.get("sales") or 0))

    if linked and anchor:
        lp, lr = linked["price"], linked.get("rating") or 0
        cheaper_better = [p for p in competitors if p["price"] <= lp and (p.get("rating") or 0) >= lr]
        top_rating = max(p.get("rating") or 0 for p in competitors)
        if cheaper_better:
            names = "、".join(p["name"] for p in cheaper_better[:2])
            lines.append(f"- 原商品值不值得买：**可以买，但不是这个价位的最优解**——{names} 更便宜且好评率不输。")
        elif lr >= top_rating and lp <= anchor * 1.12:
            lines.append("- 原商品值不值得买：**值得买**——同价位段口碑领先，没有明显贵得离谱。")
        else:
            lines.append("- 原商品值不值得买：**看需求**——没有明显价格优势，除非你认准它的品牌或生态。")
    elif linked:
        lines.append("- 原商品值不值得买：**看需求**——链接页未拿到标价，无法判断性价比，建议先在商品页确认到手价。")
    else:
        lines.append("- 原商品值不值得买：链接标题未匹配到站内同款，参数【暂无公开参数】，暂不给确定性结论。")

    reason = (best.get("highlights") or [""])[0] or (best.get("tags") or [""])[0]
    lines.append(
        f"- 优先推荐：**{best['name']}**（¥{best['price']:g}，好评 {best.get('rating') or '暂无'}%"
        f"）" + (f"，{reason}" if reason else "") + "。"
    )
    lines.append("- 价格提醒：链接标价与站内标价都以商品页为准，实际到手价会随券和活动变化。")
    return lines


def _handle_link_compare(user_id, message, url):
    info = linkparse.parse_link(url)
    title = info.get("title") or ""
    link_price = info.get("price")
    platform = info.get("platform") or "电商平台"
    linked = _match_link_product(title)
    profile = get_profile(user_id)

    brand = "、".join(parse_brands(title)) or (linked or {}).get("brand") or ""
    category = _link_category(title, linked)
    anchor_price = link_price or (linked or {}).get("price")
    competitors = _pick_competitors(linked, category, anchor_price) if category else []

    lines = ["**① 商品基础信息**", ""]
    display_name = title or (linked or {}).get("name") or "【暂无公开参数】"
    lines.append(f"- 商品名称：{display_name}")
    lines.append(f"- 品牌 / 型号：{brand or '【暂无公开参数】'} / {(linked or {}).get('name') or title or '【暂无公开参数】'}")
    lines.append(f"- 价格：{('¥%g' % link_price) if link_price else '【暂无公开参数】（页面价格需登录或动态加载，未能抓取）'}")
    lines.append(f"- 平台：{platform}")
    lines.append(f"- 购买链接：{info.get('finalUrl') or url}")
    if category:
        lines.append(f"- 所属品类：{category}")

    spec_text = (_short_specs(linked) or _info_specs_text(info)
                 or _title_specs(title))
    lines.append(f"- 核心参数：{spec_text or '【暂无公开参数】'}")
    if info.get("source"):
        src = info["source"]
        if info.get("specSource"):
            src += f"，参数来源：{info['specSource']}"
        lines.append(f"- 解析来源：{src}")

    lines.append("")
    lines.append(f"**② 同价位同类型竞品（{len(competitors)} 款）**")
    lines.append("")
    export_rows = []
    if not competitors:
        lines.append("站内暂无可比机型。")
    else:
        table_products = ([linked] if linked else []) + competitors
        # 短板判断用「表格里展示的价格」，链接价优先，避免与表格数字打架
        table_view = []
        for p in table_products:
            item = dict(p)
            if linked and link_price and p.get("id") == linked.get("id"):
                item["price"] = link_price
            table_view.append(item)
        lines.append("| 商品 | 价格 | 核心配置 | 主要优势 | 短板 | 适合人群 |")
        lines.append("|---|---|---|---|---|---|")
        export_rows = []

        if linked:
            name_cell = f"{linked['name']}（原商品）"
        else:
            name_cell = (title[:42] + "…" if len(title) > 42 else title) or "原商品"
            name_cell += "（原商品）"
        strengths = "；".join((linked or {}).get("highlights", [])[:2]) or "【暂无公开参数】"
        row = [
            name_cell,
            _price_cell(linked, link_price, original=True),
            _short_specs(linked) or _info_specs_text(info) or _title_specs(title) or "【暂无公开参数】",
            strengths,
            _weak_points(linked, table_view),
            (linked or {}).get("audience") or "【暂无公开参数】",
        ]
        export_rows.append(row)
        lines.append("| " + " | ".join(_md_cell(v) for v in row) + " |")

        for p in competitors:
            strengths = "；".join(p.get("highlights", [])[:2]) or "；".join(p.get("tags", [])[:2]) or "【暂无公开参数】"
            row = [
                p["name"],
                _price_cell(p),
                _short_specs(p) or "【暂无公开参数】",
                strengths,
                _weak_points(p, table_view),
                p.get("audience") or "【暂无公开参数】",
            ]
            export_rows.append(row)
            lines.append("| " + " | ".join(_md_cell(v) for v in row) + " |")

    lines.append("")
    lines.append("**③ 选购总结建议**")
    lines.append("")
    verdict_lines = _link_verdict(linked, link_price, competitors)
    lines.extend(verdict_lines)
    lines.append("")
    lines.append("> 说明：以上对比只用公开标价与商品页公开参数，未做任何夸大；参数缺失处均已标注【暂无公开参数】。")

    if competitors:
        lines.append("")
        lines.append("是否需要为您提供一份表格？")
        _remember_compare(user_id, {
            "title": "商品对比表",
            "headers": ["商品", "价格", "核心配置", "主要优势", "短板", "适合人群"],
            "rows": export_rows,
            "notes": [re.sub(r"\*\*", "", v.lstrip("- ")) for v in verdict_lines],
            "file_names": [row[0] for row in export_rows],
        }, [p["id"] for p in competitors])

    for p in competitors:
        record_event(user_id, "view", p["id"])

    reply = "\n".join(lines)
    quicks = []
    if competitors:
        quicks.append("需要，给我一份表格")
    if linked:
        quicks.append("看看原商品详情")
    if competitors:
        quicks.append("对比这几款")
    quicks.append("再找几款更便宜的")

    return {
        "reply": reply,
        "markdown": True,
        "narrator": "template",
        "intent": "link",
        "dialect": None,
        "parsed": {
            "normalized": message,
            "budget": None,
            "brands": [brand] if brand else [],
            "scenes": [],
            "mentions": [linked["name"]] if linked else [],
        },
        "products": [product_card(p, profile) for p in competitors],
        "compare": None,
        "plan": None,
        "quickReplies": quicks,
        "profile": profile_summary(profile),
    }


# ---------------------------------------------------------------- 主入口
def handle_chat(user_id, message, dialect_hint=None):
    link_url, _platform = linkparse.find_link(message)
    if link_url:
        return _handle_link_compare(user_id, message, link_url)
    if _is_export_request(message):
        exported = _export_reply(user_id)
        if exported:
            return exported
    parsed = parse_query(message, dialect_hint)
    parsed = _enrich_parsed(parsed)
    profile = get_profile(user_id)
    compare_data = None
    relaxed = False
    web_ids = []

    if parsed["intent"] in ("recommend", "detail", "compare"):
        live = run_web_search(parsed)
        web_ids = [p["id"] for p in live]

    if parsed["intent"] == "compare":
        compare_data = _resolve_compare(parsed, profile)
        if compare_data is None:
            parsed["intent"] = "recommend"

    if parsed["intent"] == "compare":
        cards = compare_data["products"] if compare_data else []
    elif parsed["intent"] in ("greet", "profile", "unknown"):
        cards = []
    elif parsed["intent"] == "detail":
        target = product_by_id(parsed["mentions"][0]) if parsed["mentions"] else product_by_id(web_ids[0]) if web_ids else None
        if target:
            cards = [product_card(target, profile, 96, ["你问的就是这款"])]
        else:
            parsed["intent"] = "recommend"
            cards = recommend(parsed, profile, limit=3)
    else:
        cards = recommend(parsed, profile, limit=3)
        strict_count = len(_apply_filters(parsed))
        relaxed = len(cards) > strict_count
        if not cards:
            relaxed = True
            cards = recommend({"brands": [], "scenes": [], "keywords": [], "budget": None, "mentions": [], "intent": "recommend"}, profile, limit=3)
        extra = [i for i in web_ids if all(c["id"] != i for c in cards)]
        if extra:
            web_cards = [product_card(product_by_id(i), profile, 88, ["本地库没有，联网查到的在售机型"])
                         for i in extra[:3]]
            cards = (web_cards + [c for c in cards if c["id"] not in web_ids])[:3]

    for card in cards:
        record_event(user_id, "view", card["id"])

    profile = record_search(user_id, parsed)
    if compare_data:
        for pid in compare_data["ids"]:
            record_event(user_id, "compare", pid)
        _remember_compare(user_id, _compare_export_model(compare_data), compare_data.get("ids"))

    plan = None
    if cards and parsed["intent"] == "recommend":
        plan = build_purchase_plan(cards, parsed, profile)

    reply, quicks = build_reply(parsed, cards, compare_data, profile, {"relaxed": relaxed, "plan": plan})

    # 寒暄/查记忆/没听懂这三类用固定话术，避免模型硬凑成商品列表
    llm_text = None
    if parsed["intent"] not in ("greet", "profile", "unknown"):
        llm_text = llm_narrate(parsed, cards, compare_data, profile, reply, plan)
    narrator = "llm" if llm_text else "template"
    if llm_text:
        reply = llm_text
    if compare_data and "是否需要为您提供一份表格" not in reply:
        reply = reply.rstrip() + "\n\n是否需要为您提供一份表格？"

    return {
        "reply": reply,
        "narrator": narrator,
        "intent": parsed["intent"],
        "dialect": parsed["dialect"],
        "parsed": {
            "normalized": parsed["normalized"],
            "budget": parsed["budget"],
            "brands": parsed["brands"],
            "scenes": parsed["scenes"],
            "mentions": [PRODUCT_BY_ID[i]["name"] for i in parsed["mentions"]],
        },
        "products": cards,
        "compare": compare_data,
        "plan": plan,
        "quickReplies": quicks,
        "profile": profile_summary(profile),
    }


def _resolve_compare(parsed, profile):
    mentions = parsed.get("mentions", [])
    web_ids = parsed.get("webIds", [])
    if len(mentions) >= 2:
        return build_compare(mentions[:3], profile, parsed)

    if mentions and web_ids:
        return build_compare([mentions[0], web_ids[0]], profile, parsed)

    if len(web_ids) >= 2:
        return build_compare(web_ids[:3], profile, parsed)

    if len(mentions) == 1:
        anchor = product_by_id(mentions[0])
        rival_parsed = {"brands": [], "scenes": anchor["scenes"], "keywords": [], "budget": None,
                        "mentions": [], "intent": "compare"}
        rivals = recommend(rival_parsed, profile, limit=2, exclude={anchor["id"]})
        ids = [anchor["id"]] + [r["id"] for r in rivals[:1]]
        return build_compare(ids, profile, parsed)

    brands = parsed.get("brands", [])
    if len(brands) >= 2:
        ids = []
        for b in brands[:3]:
            best = max([p for p in PRODUCTS if p["brand"] == b], key=lambda p: p["rating"] * 1000 + p["sales"] / 10)
            ids.append(best["id"])
        return build_compare(ids, profile, parsed)

    if len(brands) == 1:
        candidates = [p for p in PRODUCTS if p["brand"] == brands[0]]
        candidates.sort(key=lambda p: -p["sales"])
        if len(candidates) < 2:
            candidates = PRODUCTS
        return build_compare([candidates[0]["id"], candidates[1]["id"]], profile, parsed)

    scenes = parsed.get("scenes", [])
    if scenes:
        scored = []
        for p in PRODUCTS:
            fit = len([s for s in scenes if s in p["scenes"]])
            if fit:
                scored.append((fit, p["rating"], p))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        if len(scored) >= 2:
            return build_compare([scored[0][2]["id"], scored[1][2]["id"]], profile, parsed)

    return None


# ---------------------------------------------------------------- 可选大模型解说
LLM_CONFIG_PATH = os.path.join(DATA_DIR, "llm_config.json")


def _llm_conf():
    """优先级：运行时注入（浏览器本机）> 环境变量 > data/llm_config.json > 默认值。"""
    conf = {}
    if os.path.exists(LLM_CONFIG_PATH):
        try:
            with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except (json.JSONDecodeError, OSError):
            conf = {}
    runtime = RUNTIME_LLM or {}
    key = (runtime.get("apiKey") or os.environ.get("LLM_API_KEY")
           or os.environ.get("DEEPSEEK_API_KEY")
           or os.environ.get("OPENAI_API_KEY") or conf.get("apiKey") or "")
    base = (runtime.get("apiBase") or os.environ.get("LLM_API_BASE")
            or conf.get("apiBase") or "https://api.deepseek.com/v1").rstrip("/")
    model = runtime.get("model") or os.environ.get("LLM_MODEL") or conf.get("model") or "deepseek-chat"
    return key, base, model


def set_llm_runtime(api_key="", api_base="", model=""):
    """本机注入大模型 Key（仅进程内生效，刷新即失效，不进任何文件）。"""
    RUNTIME_LLM["apiKey"] = (api_key or "").strip()
    RUNTIME_LLM["apiBase"] = (api_base or "").strip()
    RUNTIME_LLM["model"] = (model or "").strip()
    return llm_status()


def llm_status():
    key, base, model = _llm_conf()
    masked = ""
    if key:
        masked = key[:7] + "…" + key[-4:] if len(key) > 12 else "已配置"
    return {"enabled": bool(key), "model": model if key else "", "base": base,
            "maskedKey": masked, "runtime": bool((RUNTIME_LLM or {}).get("apiKey"))}


def llm_enabled():
    return bool(_llm_conf()[0])


def llm_narrate(parsed, cards, compare_data, profile, fallback, plan=None):
    """调用 OpenAI 兼容接口润色话术；任何异常都静默回落到模板话术。"""
    if not llm_enabled():
        return None
    import urllib.error
    import urllib.request

    key, base, model = _llm_conf()

    facts = {
        "user_message": parsed.get("raw", ""),
        "user_need": _describe_needs(parsed),
        "products": [
            {"name": c["name"], "price": c["price"], "rating": c["rating"],
             "sales": c["salesText"], "tags": c["tags"], "highlights": c["highlights"],
             "web": c.get("web", False), "note": c.get("note", "")} for c in cards
        ],
        "compare": [
            {"label": r["label"], "values": r["values"]} for r in (compare_data or {}).get("rows", [])[:3]
        ],
    }
    if plan:
        facts["purchase_plan"] = {
            "budget": plan["budget"]["text"],
            "scenario": plan["scenario"]["text"],
            "primary": plan["primary"]["name"],
            "why_fit_user": plan["primary"]["why"],
            "reviews": plan["reviews"]["text"],
            "after_sales_risk": {"level": plan["risk"]["level"], "items": plan["risk"]["items"]},
            "alternatives": [f"{a['name']} ¥{a['price']}（{a['role']}）" for a in plan["alternatives"]],
            "verdict": plan["verdict"],
        }
    system = (
        "你的名字是“小函”。任何时候提到自己都自称“小函”，不要说 AI、助手、智能体、导购助手。\n"
        "你是谁不影响格式：只输出最终结果，不要开场白、寒暄、道歉、自我说明，"
        "不要重复用户问题，也不要写“好的/当然/没问题/以下是/总结一下”这类废话。\n"
        "严格按下面的纯文本格式输出，不要使用 markdown 表格或额外装饰：\n"
        "【有 purchase_plan 时】\n"
        "主推：商品名｜¥价格｜一句理由\n"
        "适合你：不超过 40 字\n"
        "评价：不超过 25 字\n"
        "售后风险：级别｜不超过 25 字\n"
        "备选：名称 ¥价格（定位）；最多 2 款\n"
        "结论：不超过 30 字\n"
        "下单前：1)… 2)… 3)…\n"
        "【普通推荐】\n"
        "结论：不超过 30 字\n"
        "推荐：\n"
        "1. 名称｜¥价格｜好评率｜一句理由（不超过 20 字）\n"
        "2. …\n"
        "3. …\n"
        "提醒：仅在有联网参考价、预算放宽或参数缺失时写一句，否则省略\n"
        "【对比或细节问答】\n"
        "结论：一句\n"
        "要点：2-3 条，每条不超过 25 字；不要重复表格内容\n"
        "硬性要求：总字数不超过 120 字（有 purchase_plan 时不超过 180 字）；"
        "只能使用给定事实，严禁编造价格、参数、销量；每款商品只出现一次；"
        "标注 web=true 的商品是联网参考价，必须提醒以下单页面为准。"
    )
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 500,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False) + "\n参考话术：" + fallback},
        ],
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def config_info():
    key, base, model = _llm_conf()
    products = _all_products()
    return {
        "products": len(products),
        "localProducts": len(PRODUCTS),
        "categories": sorted({p.get("category", "其他") for p in products}),
        "brands": sorted({p["brand"] for p in products}),
        "dialects": DIALECTS["regions"],
        "speechLanguages": DIALECTS["speechLanguages"],
        "llm": {"enabled": bool(key), "model": model, "apiBase": base},
        "webSearch": websearch.stats(),
        "database": db.info(),
    }


def product_detail(product_id, profile=None):
    p = product_by_id(product_id)
    if not p:
        return None
    card = product_card(p, profile, 0, [])
    card["detail"] = {item["label"]: item["value"] for item in _spec_items(p)}
    model_file = os.path.join(BASE_DIR, "web", "models", f"{p['id']}.glb")
    card["model3d"] = f"/models/{p['id']}.glb" if os.path.isfile(model_file) else ""
    return card
