"""The user guide behind the help button (server/routes/guide.py).

    python tests/run.py guide_routes

Locks: the real docs/README.md and docs/en/README.md parse into sections whose every page exists; a nested
"  - [..]" line becomes a child; only plain .md names inside docs/ or docs/en/ are served (no "..", no
sub-folders, no other extensions); every non-Vietnamese language reads the English pages; search ignores
Vietnamese marks and puts title matches first. No network, no main import: the router runs on a bare app.
"""
from _paths import ROOT, SERVER  # noqa: E402,F401
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.guide as guide

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _fails.append(name)


DOCS = Path(ROOT) / "docs"

# 1) The real tables of contents.
for lang, folder in (("vi", DOCS), ("en", DOCS / "en")):
    secs = guide.parse_index((folder / "README.md").read_text(encoding="utf-8"))
    items = [it for s in secs for it in s["items"]]
    children = [c for it in items for c in it["children"]]
    check(f"{lang}: the table of contents has sections and at least 30 pages",
          len(secs) >= 5 and len(items) + len(children) >= 30)
    missing = [it["name"] for it in items + children if not (folder / it["name"]).is_file()]
    check(f"{lang}: every page in the table of contents exists ({missing[:3]})", not missing)
    check(f"{lang}: the step-by-step Messenger page is a child of the Messenger page",
          any(c["name"].startswith("31-messenger-") for c in children))
    check(f"{lang}: links that leave docs/ are not pages", all("/" not in it["name"] for it in items + children))

# 2) Parsing rules on a small sample.
SAMPLE = """# Docs
## Mục lục
### Bắt đầu
- [01 - Bắt đầu](01-a.md) - mô tả một.
  - [Từng bước](01-b.md) - con.
- [Ngoài](../README.md) - không phải trang.
### Trống
## Bản tiếng Anh
- [02](02-c.md) - nằm ngoài mục lục.
"""
secs = guide.parse_index(SAMPLE)
check("sample: one non-empty section", len(secs) == 1 and secs[0]["title"] == "Bắt đầu")
check("sample: title, file and description", secs[0]["items"][0]["name"] == "01-a.md"
      and secs[0]["items"][0]["desc"] == "mô tả một.")
check("sample: an indented line is a child", secs[0]["items"][0]["children"][0]["name"] == "01-b.md")
check("sample: a link outside the folder and a list after the contents are ignored",
      len(secs[0]["items"]) == 1)

# 3) Which files are served.
tmp = Path(tempfile.mkdtemp(prefix="javis-guide-"))
(tmp / "en").mkdir()
(tmp / "README.md").write_text("## Mục lục\n### A\n- [Một](01-mot.md) - x\n", encoding="utf-8")
(tmp / "en" / "README.md").write_text("## Table of contents\n### A\n- [One](01-one.md) - x\n", encoding="utf-8")
(tmp / "01-mot.md").write_text("# Nhắc hẹn định kỳ\nĐặt lịch nhắc mỗi sáng.\n", encoding="utf-8")
(tmp / "02-hai.md").write_text("# Kênh Telegram\nNhắc hẹn gửi qua Telegram.\nNhắc hẹn lần hai.\n", encoding="utf-8")
(tmp / "en" / "01-one.md").write_text("# Reminders\nSet a reminder.\n", encoding="utf-8")
(tmp / "secret.txt").write_text("no", encoding="utf-8")
(tmp / "dev").mkdir()
(tmp / "dev" / "x.md").write_text("# dev", encoding="utf-8")

check("a plain page name is served", guide.doc_file(tmp, "vi", "01-mot.md") is not None)
for bad in ("../README.md", "dev/x.md", "secret.txt", "..\\README.md", "", "en/01-one.md", ".md"):
    check(f"refused: {bad!r}", guide.doc_file(tmp, "vi", bad) is None)
check("English reads docs/en", guide.doc_file(tmp, "en", "01-one.md") is not None
      and guide.doc_file(tmp, "en", "01-mot.md") is None)
check("German and other languages read the English pages", guide.lang_cua("de") == "en"
      and guide.lang_cua("vi-VN") == "vi" and guide.lang_cua("") == "en")

# 4) Search.
r = guide.search(tmp, "vi", "nhac hen")
check("search ignores Vietnamese marks", [x["name"] for x in r] == ["01-mot.md", "02-hai.md"])
check("a title match ranks first even with fewer hits", r[0]["name"] == "01-mot.md")
check("a result carries a snippet from the body, not the heading", r[1]["snippet"].startswith("Nhắc hẹn gửi"))
check("every word must appear", guide.search(tmp, "vi", "nhac telegram")[0]["name"] == "02-hai.md"
      and len(guide.search(tmp, "vi", "nhac telegram")) == 1)
check("an empty query finds nothing", guide.search(tmp, "vi", "  ") == [])
(tmp / "03-ba.md").write_text("# Ba\n\n***Tiếng Việt** · [English](en/03-chat.md)*\n\nMột dòng có chat.\n", encoding="utf-8")
check("the language switch line is never the snippet", guide.search(tmp, "vi", "chat")[0]["snippet"] == "Một dòng có chat.")
check("real docs: the Telegram page ranks first for 'telegram chat id'",
      guide.search(DOCS, "vi", "telegram chat id")[0]["name"] == "11-telegram.md")
check("real docs: English search works too", guide.search(DOCS, "en", "reminder")[0]["name"].startswith("08-"))
check("real docs: the English language switch line is never the snippet either",
      all("·" not in r["snippet"] or "English" not in r["snippet"] for r in guide.search(DOCS, "en", "kanban")))
check("README is never a search result", all(x["name"] != "README.md" for x in guide.search(tmp, "vi", "mot")))

# 5) The HTTP routes on a bare app.
app = FastAPI()
guide.register(app, guide.GuideDeps(docs_dir=lambda: tmp))
c = TestClient(app)
d = c.get("/guide/index?lang=vi").json()
check("GET /guide/index", d["ok"] and d["sections"][0]["items"][0]["name"] == "01-mot.md")
d = c.get("/guide/index?lang=en").json()
check("GET /guide/index in English", d["ok"] and d["lang"] == "en" and d["sections"][0]["items"][0]["name"] == "01-one.md")
d = c.get("/guide/doc?lang=vi&name=01-mot.md").json()
check("GET /guide/doc returns the markdown", d["ok"] and d["markdown"].startswith("# Nhắc hẹn"))
check("GET /guide/doc refuses a path out of docs", c.get("/guide/doc?lang=vi&name=../README.md").status_code == 404)
check("GET /guide/doc 404 for a missing page", c.get("/guide/doc?lang=vi&name=99-x.md").status_code == 404)
d = c.get("/guide/search?lang=vi&q=telegram").json()
check("GET /guide/search", d["ok"] and [x["name"] for x in d["results"]] == ["02-hai.md"])

if _fails:
    print(f"\n{len(_fails)} FAIL")
    sys.exit(1)
print("\nOK")
