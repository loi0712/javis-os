/* User guide panel: the help button next to the Notifications bell (owner request 2026-11-16).

   Opens a panel on the right with the table of contents of docs/ (server/routes/guide.py), a search box over every
   page, and the page itself rendered with mdToHtml (chat-render.js). Links between guide pages open inside the panel;
   links that leave docs/ open the same file on GitHub in a new tab. Vietnamese reads docs/, every other language
   reads docs/en/. */
(function () {
  "use strict";
  var GITHUB = "https://github.com/blogminhquy/javis-os/blob/main/docs/";
  var TEN_TRANG = /^[0-9A-Za-z][0-9A-Za-z_-]*\.md$/;

  function tw(k, vars) {
    try { if (window.t) return window.t(k, vars); } catch (e) { /* noop */ }
    return k;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function lang() {
    var l = (document.documentElement.getAttribute("lang") || "vi").toLowerCase();
    return l.indexOf("vi") === 0 ? "vi" : "en";
  }

  var btn, panel, shade, body, search, backBtn, ghLink, titleEl;
  var state = { view: "index", lang: "", name: "", index: null, timer: 0, seq: 0 };

  function build() {
    if (panel) return;
    shade = document.createElement("div");
    shade.className = "guide-shade";
    shade.hidden = true;
    panel = document.createElement("aside");
    panel.className = "guide-panel";
    panel.id = "guidePanel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "false");
    panel.hidden = true;
    panel.innerHTML =
      '<div class="guide-head">' +
        '<button class="guide-back" type="button" hidden></button>' +
        '<div class="guide-title"></div>' +
        '<a class="guide-gh" target="_blank" rel="noopener" hidden></a>' +
        '<button class="guide-close" type="button">×</button>' +
      "</div>" +
      '<div class="guide-search-row"><input class="guide-search" type="search" autocomplete="off"></div>' +
      '<div class="guide-body"></div>';
    document.body.appendChild(shade);
    document.body.appendChild(panel);
    body = panel.querySelector(".guide-body");
    search = panel.querySelector(".guide-search");
    backBtn = panel.querySelector(".guide-back");
    ghLink = panel.querySelector(".guide-gh");
    titleEl = panel.querySelector(".guide-title");
    labels();

    shade.addEventListener("click", close);
    panel.querySelector(".guide-close").addEventListener("click", close);
    backBtn.addEventListener("click", function () { search.value = ""; showIndex(); });
    search.addEventListener("input", function () {
      clearTimeout(state.timer);
      var q = search.value.trim();
      state.timer = setTimeout(function () { if (q) runSearch(q); else showIndex(); }, 250);
    });
    body.addEventListener("click", onBodyClick, true);
  }

  function labels() {
    if (!panel) return;
    panel.setAttribute("aria-label", tw("guide.title"));
    titleEl.textContent = tw("guide.title");
    search.placeholder = tw("guide.search");
    backBtn.textContent = "← " + tw("guide.contents");
    ghLink.textContent = tw("guide.github") + " ↗";
    panel.querySelector(".guide-close").setAttribute("aria-label", tw("common.close"));
  }

  function isOpen() { return !!(panel && !panel.hidden); }

  function open(name) {
    build();
    // One panel at a time: the bell's panel sits in the same corner.
    var noti = document.getElementById("notificationPanel");
    if (noti && !noti.hidden) {
      var nc = document.getElementById("notificationClose");
      if (nc) nc.click();
    }
    panel.hidden = false;
    shade.hidden = false;
    if (btn) btn.setAttribute("aria-expanded", "true");
    if (name) showDoc(name, lang());
    else if (state.lang !== lang() || state.view === "index") showIndex();
    if (state.view === "index") setTimeout(function () { try { search.focus(); } catch (e) { /* noop */ } }, 30);
  }

  function close() {
    if (!panel) return;
    panel.hidden = true;
    shade.hidden = true;
    if (btn) { btn.setAttribute("aria-expanded", "false"); try { btn.focus(); } catch (e) { /* noop */ } }
  }

  function loading() { body.innerHTML = '<div class="guide-note">' + esc(tw("guide.loading")) + "</div>"; }
  function failed() { body.innerHTML = '<div class="guide-note guide-err">' + esc(tw("guide.error")) + "</div>"; }

  function getJson(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      return r.json().then(function (d) { if (!r.ok || !d || !d.ok) throw new Error("guide"); return d; });
    });
  }

  function setHead(view, name, l) {
    state.view = view;
    backBtn.hidden = view === "index";
    ghLink.hidden = view !== "doc";
    if (view === "doc") ghLink.href = GITHUB + (l === "vi" ? "" : "en/") + name;
  }

  function showIndex() {
    var l = lang();
    setHead("index");
    if (state.index && state.lang === l) { drawIndex(state.index); return; }
    loading();
    var my = ++state.seq;
    getJson("/guide/index?lang=" + l).then(function (d) {
      if (my !== state.seq) return;
      state.index = d.sections || [];
      state.lang = l;
      drawIndex(state.index);
    }).catch(function () { if (my === state.seq) failed(); });
  }

  function itemHtml(it, child) {
    return '<button type="button" class="guide-item' + (child ? " child" : "") + '" data-doc="' + esc(it.name) + '">' +
      '<span class="guide-item-title">' + esc(it.title) + "</span>" +
      (it.desc ? '<span class="guide-item-desc">' + esc(it.desc) + "</span>" : "") + "</button>" +
      (it.children || []).map(function (c) { return itemHtml(c, true); }).join("");
  }

  function drawIndex(sections) {
    if (!sections.length) { failed(); return; }
    body.innerHTML = sections.map(function (s) {
      return '<section class="guide-sec"><h3>' + esc(s.title) + "</h3>" +
        s.items.map(function (it) { return itemHtml(it, false); }).join("") + "</section>";
    }).join("");
    body.scrollTop = 0;
  }

  function runSearch(q) {
    var l = lang();
    setHead("search");
    loading();
    var my = ++state.seq;
    getJson("/guide/search?lang=" + l + "&q=" + encodeURIComponent(q)).then(function (d) {
      if (my !== state.seq) return;
      var rs = d.results || [];
      if (!rs.length) {
        body.innerHTML = '<div class="guide-note">' + esc(tw("guide.no_results", { q: q })) + "</div>";
        return;
      }
      body.innerHTML = rs.map(function (r) {
        return '<button type="button" class="guide-item" data-doc="' + esc(r.name) + '">' +
          '<span class="guide-item-title">' + esc(r.title) + "</span>" +
          (r.snippet ? '<span class="guide-item-desc">' + esc(r.snippet) + "</span>" : "") + "</button>";
      }).join("");
      body.scrollTop = 0;
    }).catch(function () { if (my === state.seq) failed(); });
  }

  // A link inside a page: [lang, name, anchor] when it points to another guide page, else null.
  function guideTarget(href, l) {
    var h = String(href || "").trim().replace(/^\.\//, "");
    if (!h || /^(https?:|mailto:|data:|\/)/i.test(h)) return null;
    var anchor = "";
    var i = h.indexOf("#");
    if (i >= 0) { anchor = h.slice(i + 1); h = h.slice(0, i); }
    if (!h) return null;
    var tl = l;
    if (l === "vi" && h.indexOf("en/") === 0) { tl = "en"; h = h.slice(3); }
    else if (l === "en" && h.indexOf("../") === 0 && h.slice(3).indexOf("/") < 0) { tl = "vi"; h = h.slice(3); }
    if (!TEN_TRANG.test(h)) return null;
    return [tl, h, anchor];
  }

  // Links that leave docs/ (../README.md, dev/...) point at the same file on GitHub instead of the user's brain.
  function githubUrl(href, l) {
    var h = String(href || "").trim().replace(/^\.\//, "");
    var base = GITHUB + (l === "vi" ? "" : "en/");
    try { return new URL(h, base).href; } catch (e) { return base; }
  }

  // Rewrite links before mdToHtml so they never resolve against the user's brain. Code is left as written: the guide
  // shows syntax examples like `![x](attachments/a.png)` that must read exactly so.
  function prepare(md, l) {
    return String(md || "").split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/).map(function (part, i) {
      return i % 2 ? part : prepareText(part, l);
    }).join("");
  }
  function prepareText(md, l) {
    return md.replace(/(!?)\[([^\]]*)\]\(([^()\s]+)(\s+"[^"]*")?\)/g, function (m, bang, text, href) {
      if (/^(https?:|mailto:|#)/i.test(href)) return m;
      var g = guideTarget(href, l);
      if (g && !bang) {
        return "[" + text + "](/guide/doc?lang=" + g[0] + "&name=" + encodeURIComponent(g[1]) +
          (g[2] ? "&anchor=" + encodeURIComponent(g[2]) : "") + ")";
      }
      return bang + "[" + text + "](" + githubUrl(href, l) + ")";
    });
  }

  function showDoc(name, l, anchor) {
    setHead("doc", name, l);
    loading();
    var my = ++state.seq;
    getJson("/guide/doc?lang=" + l + "&name=" + encodeURIComponent(name)).then(function (d) {
      if (my !== state.seq) return;
      var md = prepare(d.markdown, d.lang);
      var html = window.mdToHtml ? window.mdToHtml(md, "", { trinhSua: true }) : "<pre>" + esc(md) + "</pre>";
      body.innerHTML = '<article class="guide-doc ne-prev">' + html + "</article>";
      setHead("doc", d.name, d.lang);
      body.scrollTop = 0;
      if (anchor) scrollToAnchor(anchor);
    }).catch(function () { if (my === state.seq) failed(); });
  }

  function slug(s) {
    return String(s || "").toLowerCase().trim().replace(/[^\p{L}\p{N}\s-]/gu, "").replace(/\s+/g, "-");
  }

  function scrollToAnchor(anchor) {
    var want = decodeURIComponent(anchor).toLowerCase();
    var hs = body.querySelectorAll("h1,h2,h3,h4");
    for (var i = 0; i < hs.length; i++) {
      if (slug(hs[i].textContent) === want || slug(hs[i].textContent) === want.replace(/^-+/, "")) {
        hs[i].scrollIntoView({ block: "start" });
        return;
      }
    }
  }

  function onBodyClick(e) {
    var item = e.target.closest && e.target.closest("[data-doc]");
    if (item) { e.preventDefault(); showDoc(item.getAttribute("data-doc"), lang()); return; }
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (href.indexOf("/guide/doc?") === 0) {
      e.preventDefault();
      e.stopPropagation();
      var u = new URL(href, location.origin);
      showDoc(u.searchParams.get("name"), u.searchParams.get("lang") || lang(), u.searchParams.get("anchor") || "");
      return;
    }
    if (href.charAt(0) === "#") {
      e.preventDefault();
      scrollToAnchor(href.slice(1));
    }
  }

  function init() {
    btn = document.getElementById("guideTrigger");
    if (!btn) return;
    btn.addEventListener("click", function () { if (isOpen()) close(); else open(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && isOpen()) close(); });
    window.addEventListener("javis:i18n", function () {
      labels();
      if (isOpen() && state.view === "index") showIndex();
    });
  }

  window.JavisGuide = { open: open, close: close, isOpen: isOpen, _guideTarget: guideTarget, _prepare: prepare };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
