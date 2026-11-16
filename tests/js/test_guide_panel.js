/* User guide panel next to the Notifications bell (guide-panel.js, server/routes/guide.py).

       node tests/js/test_guide_panel.js

   Locks the wiring that breaks silently: the button sits right before the bell (also on a phone, where
   mobile-chat.js moves the bell into the header), its files load, every key it uses exists, and links inside a
   guide page open the right page in the panel instead of resolving against the user's brain. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.join(__dirname, "..", "..");
const rd = (f) => fs.readFileSync(path.join(ROOT, f), "utf8");
const JS = rd("dashboard/guide-panel.js");
const IDX = rd("dashboard/index.html");
const MOB = rd("dashboard/mobile-chat.js");
const VI = JSON.parse(rd("dashboard/i18n/vi.json"));
const EN = JSON.parse(rd("dashboard/i18n/en.json"));

const fails = [];
const check = (ten, ok, them) => {
  console.log((ok ? "ok   " : "FAIL ") + ten + (ok || them === undefined ? "" : "  [" + them + "]"));
  if (!ok) fails.push(ten);
};

// 1. The button and its files
const gi = IDX.indexOf('id="guideTrigger"');
const ni = IDX.indexOf('id="notificationTrigger"');
check("the guide button exists in the right-hand group", gi > 0 && IDX.lastIndexOf('class="hud-top-right"', gi) > 0);
const nextBtn = IDX.indexOf("<button", IDX.indexOf("</button>", gi));
check("the guide button comes right before the bell", gi < ni && /^<button[^>]*id="notificationTrigger"/.test(IDX.slice(nextBtn)));
check("the button has a translated tooltip and label", /id="guideTrigger"[\s\S]{0,300}data-i18n-title="guide\.open"[\s\S]{0,200}data-i18n-aria="guide\.open"/.test(IDX));
check("guide-panel.css and guide-panel.js load", /\/static\/guide-panel\.css\?v=\d+/.test(IDX) && /\/static\/guide-panel\.js\?v=\d+/.test(IDX));
check("guide-panel.js loads after chat-render.js (mdToHtml)", IDX.indexOf("/static/chat-render.js") < IDX.indexOf("/static/guide-panel.js"));
check("on a phone the guide button follows the bell", /getElementById\("guideTrigger"\)[\s\S]{0,300}insertBefore\(guideTrigger, notificationTrigger\)/.test(MOB));

// 2. Every key the panel uses exists in both languages
const keys = Array.from(new Set((JS.match(/tw\("([a-z_.]+)"/g) || []).map((m) => m.slice(4, -1)))).concat(["guide.open"]);
const missing = keys.filter((k) => !(k in VI) || !(k in EN));
check("every key exists in vi.json and en.json", keys.length >= 7 && missing.length === 0, missing.join(", "));
check("no em dash in the new strings", keys.every((k) => !/—/.test(VI[k] || "") && !/—/.test(EN[k] || "")));

// 3. Link handling, run in a bare window
const win = { addEventListener() {}, location: { origin: "http://x" } };
const doc = { readyState: "complete", getElementById: () => null, documentElement: { getAttribute: () => "vi" },
  addEventListener() {} };
vm.runInNewContext(JS, { window: win, document: doc, setTimeout, clearTimeout, URL, fetch: () => Promise.reject() });
const G = win.JavisGuide;
check("the panel exposes open and close", G && typeof G.open === "function" && typeof G.close === "function");
const t = (h, l) => JSON.stringify(G._guideTarget(h, l));
check("a page in the same folder", t("21-viec-kanban.md", "vi") === '["vi","21-viec-kanban.md",""]');
check("the language switch from Vietnamese", t("en/21-kanban-work.md", "vi") === '["en","21-kanban-work.md",""]');
check("the language switch from English", t("../21-viec-kanban.md", "en") === '["vi","21-viec-kanban.md",""]');
check("an anchor is kept", t("17-khac-phuc-su-co.md#loi", "vi") === '["vi","17-khac-phuc-su-co.md","loi"]');
check("a file outside docs is not a guide page", G._guideTarget("../README.md", "vi") === null
  && G._guideTarget("dev/them-mot-ngon-ngu.md", "vi") === null && G._guideTarget("https://x.y/a.md", "vi") === null);

const p = G._prepare("Xem [Kanban](21-viec-kanban.md), [EN](en/21-kanban-work.md), [Dev](dev/x.md), [Web](https://a.b), [Gốc](../CLAUDE.md#x).", "vi");
check("a guide link opens in the panel", p.indexOf("[Kanban](/guide/doc?lang=vi&name=21-viec-kanban.md)") >= 0);
check("the language switch opens the English page in the panel", p.indexOf("[EN](/guide/doc?lang=en&name=21-kanban-work.md)") >= 0);
check("a link that leaves docs goes to GitHub, never to the brain",
  p.indexOf("[Dev](https://github.com/blogminhquy/javis-os/blob/main/docs/dev/x.md)") >= 0
  && p.indexOf("[Gốc](https://github.com/blogminhquy/javis-os/blob/main/CLAUDE.md#x)") >= 0);
check("an outside URL is left alone", p.indexOf("[Web](https://a.b)") >= 0);
const code = "Viết `![x](attachments/a.png)` rồi [Kanban](21-viec-kanban.md).\n```\n[y](../z.md)\n```\n";
const pc = G._prepare(code, "vi");
check("inline code is left exactly as written", pc.indexOf("`![x](attachments/a.png)`") >= 0);
check("a fenced block is left exactly as written", pc.indexOf("```\n[y](../z.md)\n```") >= 0);
check("a link next to code is still rewritten", pc.indexOf("(/guide/doc?lang=vi&name=21-viec-kanban.md)") >= 0);

if (fails.length) { console.log("\nFAIL: " + fails.length); process.exit(1); }
console.log("\nOK");
