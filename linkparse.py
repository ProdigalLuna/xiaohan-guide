"""淘宝 / 京东商品链接解析（纯标准库，无第三方依赖）。

解析顺序：
1. 直接抓取商品页，提取 og:title / <title> / meta description / 价格字段；
2. 页面是骨架屏或被反爬拦截时，用搜索引擎按商品 ID 反查标题与价格；
3. 仍拿不到的字段返回空值，由调用方标注【暂无公开参数】。
"""

import html
import json
import re
import time
import urllib.parse
import urllib.request

import websearch

URL_RE = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)?"
                    r"(taobao\.com|tmall\.com|tb\.cn|jd\.com|3\.cn)[^\s\u4e00-\u9fa5]*", re.I)

PLATFORM_LABEL = {"taobao": "淘宝", "jd": "京东"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def find_link(text):
    """从一段文本里找出第一个淘宝/京东链接。"""
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:)]}>\"'")
        if not url.lower().startswith("http"):
            url = "https://" + url
        return url, _platform(url)
    return None, None


def _platform(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    if "jd.com" in host or host.endswith("3.cn"):
        return "jd"
    if "taobao.com" in host or "tmall.com" in host or "tb.cn" in host:
        return "taobao"
    return None


def _clean_title(title):
    title = html.unescape(title or "").strip()
    title = re.sub(r"【[^】]*行情[^】]*】|【[^】]*报价[^】]*】|【[^】]*图片[^】]*评论[^】]*】", "", title)
    title = re.sub(r"(京东购物-商品详情|京东\(JD\.COM\).*|淘宝网.*|天猫.*|淘！我喜欢.*)", "", title)
    title = re.split(r"[-_|]\s*(?:京东|淘宝|天猫)", title)[0]
    title = re.sub(r"\s+", " ", title).strip(" -|_")
    title = title.replace("...", "").strip()
    return title.strip()


def _meta(html_text, names):
    for name in names:
        pattern = re.compile(r"<meta[^>]+>", re.I)
        for tag in pattern.findall(html_text):
            if re.search(r'(?:name|property)=["\']%s["\']' % re.escape(name), tag, re.I):
                m = re.search(r'content=["\']([^"\']*)["\']', tag, re.I)
                if m:
                    return html.unescape(m.group(1)).strip()
    return ""


def _fetch(url, timeout=12, mobile=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA_MOBILE if mobile else UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/json,application/xhtml+xml",
        "Referer": "https://item.jd.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.geturl(), raw.decode(charset, "ignore")


PRICE_PATTERNS = [
    r'"p":"([\d.]+)"',
    r'"price":"([\d.]+)"',
    r'"jdPrice":"([\d.]+)"',
    r'"lowestPrice":([\d.]+)',
    r'"salePrice":([\d.]+)',
]

SNIPPET_PRICE_PATTERNS = PRICE_PATTERNS + [
    r'[¥￥]\s*([\d,]+(?:\.\d+)?)',
    r'到手价[：: ]?\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)',
]


def _price_from(text, patterns=None):
    if not text:
        return None
    for pattern in (patterns or PRICE_PATTERNS):
        for m in re.finditer(pattern, text):
            raw = m.group(1).replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                continue
            if 1 <= value <= 1000000:
                return value
    return None


def _price_from_snippets(results):
    for item in results:
        blob = f"{item.get('title', '')} {item.get('snippet', '')}"
        price = _price_from(blob, SNIPPET_PRICE_PATTERNS)
        if price:
            return price
    return None


def _search_title(platform, item_id):
    label = PLATFORM_LABEL[platform]
    domain = "item.jd.com" if platform == "jd" else "item.taobao.com"
    queries = []
    if item_id:
        queries.append(f"site:{domain} {item_id}")
        queries.append(f"{item_id} {label} 商品 价格")
    for query in queries:
        try:
            results, _provider = websearch.search(query, n=5)
        except Exception:
            continue
        if not results:
            continue
        picked = None
        for item in results:
            title = _clean_title(item.get("title", ""))
            url = item.get("url", "")
            if not title or len(title) < 4:
                continue
            if item_id and (item_id in url or item_id in title):
                picked = title
                break
            if not picked and (label in title or label in item.get("snippet", "")):
                picked = title
        if picked:
            return picked, _price_from_snippets(results)
    return "", None


def _item_id(platform, url):
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    for key in ("id", "itemId", "sku", "wareId"):
        if qs.get(key):
            return qs[key][0]
    m = re.search(r"/(\d{8,})\.html", parsed.path)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{8,})", parsed.path)
    if m:
        return m.group(1)
    return ""


def _jd_price_api(item_id):
    if not item_id:
        return None
    try:
        _, text = _fetch(f"https://p.3.cn/prices/mgets?skuIds=J_{item_id}", timeout=8)
        return _price_from(text)
    except Exception:
        return None


# ---------------------------------------------------------------- 参数补全
SPEC_TTL = 1800
_SPEC_CACHE = {}

SPEC_LABELS = [
    ("处理器", ["处理器", "cpu型号", "cpu频率", "cpu"]),
    ("屏幕", ["屏幕尺寸", "主屏尺寸", "屏幕大小", "屏幕"]),
    ("电池", ["电池容量", "电池类型", "电池"]),
    ("充电", ["有线充电", "充电功率", "快充", "充电"]),
    ("影像", ["像素", "后置摄像头", "摄像头", "影像"]),
    ("内存", ["运行内存", "ram容量", "ram"]),
    ("存储", ["机身存储", "rom容量", "存储容量", "存储"]),
    ("重量", ["机身重量", "整机重量", "重量"]),
    ("系统", ["操作系统", "系统版本", "系统"]),
]

SPEC_SKIP = ("型号", "价格", "商家", "在售", "上市日期", "保修", "包装清单", "更多参数")
SPEC_SKIP_EXACT = {"型号", "产品型号", "上市日期", "国内发布时间", "国外发布时间",
                   "电商报价", "价格/商家", "更多参数"}

SPEC_EXTRACT = {
    "屏幕": r"(?<![\d.])\d{1,2}(?:\.\d+)?\s*(?:英寸|寸)",
    "电池": r"\d{3,5}\s*mAh",
    "充电": r"\d{2,3}(?:\.\d+)?\s*[Ww]",
    "影像": r"\d+(?:\.\d+)?\s*(?:万像素|MP)",
    "内存": r"\d{1,2}\s*GB",
    "存储": r"\d{2,4}\s*(?:GB|TB)",
    "重量": r"\d{2,3}\s*(?:g|克)",
}

SPEC_NOISE = ("官方旗舰店", "旗舰店", "自营", "正品", "新品", "现货", "预售", "包邮",
              "全国联保", "双卡双待", "全网通", "全新", "未拆封", "手机",
              "黑色", "白色", "蓝色", "绿色", "灰色", "银色", "金色", "紫色", "粉色", "橙色")

SPEC_TEXT_PATTERNS = {
    "处理器": r"(?:骁龙|天玑|麒麟|猎户座|Exynos)\s*[0-9A-Za-z至尊版（）()]{1,12}",
    "屏幕": r"\d{2}(?:\.\d+)?\s*(?:英寸|寸)",
    "电池": r"\d{3,5}\s*mAh",
    "充电": r"\d{2,3}\s*W(?:\s*(?:有线|快充|闪充|无线))?",
    "内存": r"\d{1,2}\s*GB\s*(?:内存|RAM|运行内存)",
    "存储": r"\d{2,4}\s*(?:GB|TB)\s*(?:存储|ROM|机身存储)",
    "重量": r"\d{2,3}\s*(?:g|克)",
    "系统": r"(?:iOS|Android|HarmonyOS|鸿蒙)\s*\d*(?:\.\d+)?",
}

SEO_BLOB_WORDS = (
    "京东JD.COM是国内专业的网上购物商城",
    "为您提供", "淘宝网", "淘！我喜欢", "登录", "验证",
)


def _plain(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_spec_value(value):
    text = re.sub(r"纠错|查看外观|手机性能排行|游戏运行流畅|大电池", " ", value or "")
    text = re.split(r"\s*更多|\s*查看", text)[0]
    text = text.replace(">", " ").replace("＞", " ")
    return re.sub(r"\s+", " ", text).strip()[:30]


def _specs_from_param_page(page):
    """解析中关村在线等参数页的 <th>参数名</th><td>参数值</td> 结构。"""
    raw, seen = [], set()
    for block in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S | re.I):
        m = re.search(r"<th[^>]*>(.*?)</th>", block, re.S | re.I)
        if not m:
            continue
        label = _plain(m.group(1))
        if not label or label in seen:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", block, re.S | re.I)
        if not tds:
            continue
        value = _plain(tds[0])
        if not value:
            continue
        if label in SPEC_SKIP_EXACT:
            continue
        if any(word in label for word in ("价格", "商家", "在售", "上市日期", "保修", "包装清单")):
            continue
        seen.add(label)
        raw.append((label, _clean_spec_value(value)))

    specs = {}
    for canonical, aliases in SPEC_LABELS:
        for alias in aliases:
            found = ""
            for label, value in raw:
                if alias not in label.replace(" ", "").lower():
                    continue
                if alias == "像素" and "密度" in label:
                    continue
                found = value
                break
            if not found:
                continue
            pattern = SPEC_EXTRACT.get(canonical)
            if pattern:
                m = re.search(pattern, found, re.I)
                if not m:
                    continue
                found = re.sub(r"\s+", "", m.group(0))
                if canonical == "充电":
                    found = found.replace("w", "W")
            specs[canonical] = found
            break
    return specs


BRAND_PREFIXES = ("苹果", "Apple", "小米", "红米", "Redmi", "华为", "荣耀",
                  "OPPO", "vivo", "一加", "三星", "Samsung", "真我", "realme", "魅族")


def _strip_brand(text):
    for brand in BRAND_PREFIXES:
        if (text or "").startswith(brand):
            return text[len(brand):].strip()
    return text


def _spec_query(title):
    """把营销标题压成适合搜索的参数查询词。"""
    text = re.sub(r"[（(][^）)]*[）)]", " ", title or "")
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:GB|TB|G|T)\b", " ", text, flags=re.I)
    text = re.sub(r"([\u4e00-\u9fff])([A-Za-z])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z])([\u4e00-\u9fff])", r"\1 \2", text)
    text = re.split(r"支持", text)[0]
    for word in SPEC_NOISE:
        text = text.replace(word, " ")
    text = re.sub(r"[【】\[\]（）()，,、|/]+", " ", text)
    parts = [p for p in text.split() if p]
    return " ".join(parts[:4]) or (title or "")[:24]



def _specs_from_text(text):
    specs = {}
    for canonical, pattern in SPEC_TEXT_PATTERNS.items():
        m = re.search(pattern, text or "", re.I)
        if m:
            value = re.sub(r"\s+", " ", m.group(0)).strip()
            if value:
                specs[canonical] = value
    return specs


def _order_specs(specs):
    ordered = {}
    for canonical, _aliases in SPEC_LABELS:
        if specs.get(canonical):
            ordered[canonical] = specs[canonical]
    for key, value in specs.items():
        if key not in ordered and value:
            ordered[key] = value
    return ordered


def format_specs(specs):
    return "；".join(f"{k} {v}" for k, v in (specs or {}).items() if v)


def enrich_specs(title):
    """用搜索引擎找参数页 / 摘要，补齐链接商品的核心参数，返回 (参数, 来源)。"""
    key = re.sub(r"\s+", " ", title or "").strip()[:80]
    if len(key) < 4:
        return {}, ""
    now = time.time()
    hit = _SPEC_CACHE.get(key)
    if hit and now - hit[0] < SPEC_TTL:
        return hit[1], hit[2]

    core = _spec_query(key)
    bases = []
    stripped = _strip_brand(core)
    if stripped != core and re.search(r"[A-Za-z]{3,}", stripped):
        bases.append(stripped)
    bases.append(core)
    if key not in bases:
        bases.append(key)

    queries = []
    for base in bases:
        for suffix in (" 参数", " 配置"):
            query = (base + suffix).strip()
            if query not in queries and len(query) >= 5:
                queries.append(query)

    items, zol_items = [], []
    for query in queries:
        try:
            items = websearch._bing(query, n=8)
        except Exception:
            items = []
        if not items:
            continue
        zol_items = [i for i in items
                     if "zol.com.cn" in i.get("url", "") and "param" in i.get("url", "")]
        if zol_items:
            break

    if not items and queries:
        try:
            items, _provider = websearch.search(queries[0], n=6)
        except Exception:
            items = []

    specs, source = {}, ""
    for item in zol_items:
        try:
            _, page = _fetch(item["url"], timeout=12)
            found = _specs_from_param_page(page)
        except Exception:
            found = {}
        if len(found) >= 3:
            specs, source = found, "中关村在线"
            break

    if len(specs) < 3:
        blob = " ".join(f"{i.get('title', '')} {i.get('snippet', '')}" for i in items)
        from_text = _specs_from_text(blob)
        if from_text:
            specs = dict(from_text, **specs) if specs else from_text
            source = source or "搜索摘要"

    specs = _order_specs(specs)
    _SPEC_CACHE[key] = (now, specs, source)
    if len(_SPEC_CACHE) > 300:
        for k in list(_SPEC_CACHE)[:100]:
            _SPEC_CACHE.pop(k, None)
    return specs, source


def _is_seo_blob(text):
    return any(word in (text or "") for word in SEO_BLOB_WORDS)



def _valid_title(title):
    if not title or len(title) < 6:
        return False
    generic = ["京东购物", "商品详情", "淘宝网", "天猫", "登录", "验证", "404"]
    return not any(g in title for g in generic)


def parse_link(url):
    platform = _platform(url) or ""
    result = {
        "url": url,
        "finalUrl": "",
        "platform": PLATFORM_LABEL.get(platform, ""),
        "itemId": "",
        "title": "",
        "price": None,
        "spec": "",
        "specs": {},
        "specSource": "",
        "source": "",
    }
    if not platform:
        return result

    page_title = ""
    page_spec = ""
    page_price = None
    final_url = url
    try:
        final_url, page = _fetch(url, mobile=True)
        og_title = _meta(page, ["og:title", "twitter:title"])
        if not og_title:
            m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
            og_title = html.unescape(m.group(1)) if m else ""
        page_title = _clean_title(og_title)
        page_spec = _meta(page, ["description", "og:description"])
        page_price = _price_from(page)
        if platform == "jd":
            page_price = page_price or _jd_price_api(_item_id(platform, final_url))
    except Exception:
        pass

    result["finalUrl"] = final_url
    result["itemId"] = _item_id(platform, final_url) or _item_id(platform, url)

    title = page_title if _valid_title(page_title) else ""
    price = page_price
    source = "商品页" if title else ""

    if not title:
        searched, searched_price = _search_title(platform, result["itemId"])
        if searched:
            title = searched
            source = "搜索引擎"
            price = price or searched_price

    result["title"] = title
    result["price"] = price

    if title:
        specs, spec_source = enrich_specs(title)
        result["specs"] = specs
        result["specSource"] = spec_source
        result["spec"] = format_specs(specs)

    if not result["spec"] and page_spec and len(page_spec) >= 8 and not _is_seo_blob(page_spec):
        result["spec"] = page_spec[:180]

    result["source"] = source or "解析失败"
    return result


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:] or ["https://item.jd.com/100012043978.html"]:
        print(json.dumps(parse_link(arg), ensure_ascii=False, indent=2))
