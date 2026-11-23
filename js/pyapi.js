// 浏览器端 API：用 Pyodide 在页面里跑 Python 引擎，就地接管 /api/* 与 /download/*。
// 整个站点因此是纯静态的，可直接放在 GitHub Pages 上。
(function () {
  "use strict";

  var PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
  var FILES = [
    "engine.py",
    "smart_agent.py",
    "db.py",
    "linkparse.py",
    "xlsx_export.py",
    "websearch.py",
    "bridge.py",
    "data/catalog.json",
    "data/products.json",
    "data/dialects.json"
  ];

  var base = (function () {
    var s = document.currentScript && document.currentScript.src;
    if (!s) return location.pathname.replace(/[^/]*$/, "");
    return s.replace(/js\/pyapi\.js[\s\S]*$/, "");
  })();

  var realFetch = window.fetch ? window.fetch.bind(window) : null;
  var bridgeHandle = null;
  var readyResolve;
  var ready = new Promise(function (resolve) {
    readyResolve = resolve;
  });

  // ------------------------------------------------------------ 加载遮罩
  var overlay = document.createElement("div");
  overlay.id = "pyLoadingMask";
  overlay.style.cssText = [
    "position:fixed", "inset:0", "z-index:9999", "display:flex",
    "align-items:center", "justify-content:center",
    "background:rgba(24,26,38,.93)", "color:#fff",
    "font:15px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif",
    "text-align:center", "padding:24px", "transition:opacity .35s ease"
  ].join(";");
  overlay.innerHTML =
    '<div><div style="font-size:22px;font-weight:700;margin-bottom:10px">小函智能导购</div>' +
    '<div id="pyLoadingText" style="opacity:.85">正在装载对话引擎（首次约 10–20 秒）…</div></div>';
  document.addEventListener("DOMContentLoaded", function () {
    document.body.appendChild(overlay);
  });

  function setText(text) {
    var el = document.getElementById("pyLoadingText");
    if (el) el.textContent = text;
  }

  function hideOverlay() {
    if (!overlay.parentNode) return;
    overlay.style.opacity = "0";
    setTimeout(function () {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }, 400);
  }

  // ------------------------------------------------------------ Pyodide
  function script(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = function () {
        reject(new Error("无法加载 " + src + "（需要能访问 cdn.jsdelivr.net）"));
      };
      document.head.appendChild(s);
    });
  }

  async function boot() {
    await script(PYODIDE_URL + "pyodide.js");
    setText("下载 Python 运行时…");
    var mod = await loadPyodide({ indexURL: PYODIDE_URL });

    // Pyodide 的 sqlite3 是独立包，必须单独装载，否则 db.py 导入时会报 ModuleNotFoundError
    setText("装载数据库模块…");
    await mod.loadPackage("sqlite3", { messageCallback: function () {} });

    setText("解压项目源码…");
    await mod.runPython([
      "import os, sys",
      "os.makedirs('/app/data', exist_ok=True)",
      "os.makedirs('/tmp_write', exist_ok=True)",
      "sys.path.insert(0, '/app')",
      "os.environ['APP_WRITE_DIR'] = '/tmp_write'"
    ].join("\n"));

    for (var i = 0; i < FILES.length; i++) {
      var name = FILES[i];
      var text = await (await realFetch(base + name, { cache: "no-store" })).text();
      mod.FS.writeFile("/app/" + name, text);
    }

    setText("启动推荐引擎…");
    await mod.runPython(
      "import bridge\nfrom bridge import handle_json as bridge_handle\nbridge.warmup()"
    );
    bridgeHandle = mod.globals.get("bridge_handle");
    readyResolve(true);
    hideOverlay();
  }

  boot().catch(function (err) {
    setText("引擎加载失败：" + (err && err.message ? err.message : err));
    overlay.style.background = "rgba(120,20,30,.95)";
    readyResolve(false);
    window.__PY_ERROR__ = String(err && err.message ? err.message : err);
  });

  // ------------------------------------------------------------ 响应封装
  function toResponse(payload) {
    if (!payload) {
      return new Response(JSON.stringify({ error: "engine not ready" }), {
        status: 503,
        headers: { "Content-Type": "application/json; charset=utf-8" }
      });
    }
    var status = payload.status || 200;
    var ct = payload.content_type || "application/json; charset=utf-8";
    var bodyText = payload.body || "";
    if (payload.binary) {
      var bin = atob(bodyText);
      var bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return new Response(bytes, { status: status, headers: { "Content-Type": ct } });
    }
    return new Response(bodyText, { status: status, headers: { "Content-Type": ct } });
  }

  async function callBridge(method, target, body) {
    if (!(await ready) || !bridgeHandle) return toResponse(null);
    var raw = bridgeHandle(method, target, body || "");
    var text = raw && raw.toString ? raw.toString() : String(raw);
    return toResponse(JSON.parse(text));
  }

  // ------------------------------------------------------------ 浏览器端联网
  // Pyodide 沙箱里的 Python 不能联网（socket 被禁用 + 跨域限制），
  // 因此商品链接的抓取由浏览器原生 fetch 通过公共 CORS 代理完成，
  // 抓到的原始文本放进 payload._net 交给 Python 引擎解析。
  var SHOP_RE = /(?:https?:\/\/)?(?:[a-z0-9-]+\.)?(?:jd\.com|3\.cn|taobao\.com|tmall\.com|tb\.cn)[^\s\u4e00-\u9fa5<>"']*/i;
  var NET_PROXIES = [
    function (u) { return "https://api.allorigins.win/raw?url=" + encodeURIComponent(u); },
    function (u) { return "https://api.codetabs.com/v1/proxy?quest=" + encodeURIComponent(u); },
    function (u) { return "https://corsproxy.io/?" + encodeURIComponent(u); }
  ];

  async function netText(url, ms) {
    if (!realFetch) return "";
    for (var i = 0; i < NET_PROXIES.length; i++) {
      var ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
      var timer = ctl ? setTimeout(function () { ctl.abort(); }, ms || 7000) : null;
      try {
        var opts = { cache: "no-store" };
        if (ctl) opts.signal = ctl.signal;
        var resp = await realFetch(NET_PROXIES[i](url), opts);
        if (timer) clearTimeout(timer);
        if (resp && resp.ok) {
          var text = await resp.text();
          // allorigins 的 /get 端点返回 {contents:"..."}，这里统一取出正文
          if (text && text.charAt(0) === "{") {
            try {
              var obj = JSON.parse(text);
              if (obj && typeof obj.contents === "string") text = obj.contents;
            } catch (e) { /* 保留原文本 */ }
          }
          if (text && text.length > 60) return text;
        }
      } catch (e) {
        if (timer) clearTimeout(timer);
      }
    }
    return "";
  }

  function shopItemId(url) {
    var m = url.match(/[?&](?:id|itemId|sku|wareId)=(\d{6,})/i)
         || url.match(/\/(\d{8,})\.html/)
         || url.match(/\/(\d{8,})/);
    return m ? m[1] : "";
  }

  async function prefetchLink(message) {
    var m = (message || "").match(SHOP_RE);
    if (!m) return null;
    var url = m[0].replace(/[.,;:)\]"']+$/, "");
    if (!/^https?:/i.test(url)) url = "https://" + url;
    var id = shopItemId(url);
    var isJD = /jd\.com|3\.cn/i.test(url);
    var out = { url: url, itemId: id, platform: isJD ? "jd" : "taobao", page: "", search: "" };
    out.page = await netText(url, 7000);
    if (id) {
      var label = isJD ? "京东" : "淘宝";
      var q = encodeURIComponent(label + " " + id + " 商品 型号 参数");
      out.search = await netText("https://www.bing.com/search?q=" + q + "&count=10&setlang=zh-CN", 7000);
    }
    return out;
  }

  // ------------------------------------------------------------ fetch 接管
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var parsed;
    try {
      parsed = new URL(url, location.href);
    } catch (e) {
      return realFetch ? realFetch(input, init) : Promise.reject(e);
    }
    var p = parsed.pathname;
    if (p.indexOf("/api/") !== -1 || p.indexOf("/download/") !== -1) {
      var method = (init && init.method) || (input && input.method) || "GET";
      var body = (init && init.body) || "";
      if (typeof body !== "string") body = "";
      if (p.indexOf("/api/chat") !== -1 && String(method).toUpperCase() === "POST") {
        return (async function () {
          try {
            var payload = JSON.parse(body);
            var net = await prefetchLink(payload.message || "");
            if (net) payload._net = net;
            body = JSON.stringify(payload);
          } catch (e) { /* 预抓取失败不影响对话 */ }
          return callBridge(String(method).toUpperCase(), parsed.pathname + parsed.search, body);
        })();
      }
      return callBridge(String(method).toUpperCase(), parsed.pathname + parsed.search, body);
    }
    return realFetch ? realFetch(input, init) : Promise.reject(new Error("fetch unavailable"));
  };

  // ------------------------------------------------------------ 导出下载接管
  document.addEventListener(
    "click",
    function (ev) {
      var el = ev.target;
      while (el && el.tagName !== "A") el = el.parentElement;
      if (!el) return;
      var href = el.getAttribute("href") || "";
      if (!href || href.indexOf("/download/") === -1) return;
      ev.preventDefault();
      var parsed = new URL(href, location.href);
      ready.then(function (ok) {
        if (!ok) {
          alert("引擎未加载完成，暂时无法导出表格。");
          return;
        }
        callBridge("GET", parsed.pathname + parsed.search, "").then(function (res) {
          return res.arrayBuffer().then(function (buf) {
            var name = el.getAttribute("download") || parsed.pathname.split("/").pop();
            var blob = new Blob([buf], {
              type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            });
            var a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = name;
            document.body.appendChild(a);
            a.click();
            setTimeout(function () {
              URL.revokeObjectURL(a.href);
              a.remove();
            }, 1500);
          });
        });
      });
    },
    true
  );
})();
