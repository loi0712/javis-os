"""User guide inside the dashboard: the help button next to the Notifications bell (owner request 2026-11-16).

    GET /guide/index?lang=vi|en      the table of contents parsed from docs/README.md (or docs/en/README.md)
    GET /guide/doc?lang=&name=       the raw markdown of one guide page; the dashboard renders it with mdToHtml
    GET /guide/search?lang=&q=       pages whose text matches q, best first, with one matching line each

The pages are the same docs/*.md files the repo ships, read from disk, so the guide always matches the
version that is running and works on a machine with no Internet. Vietnamese pages live in docs/, English
ones in docs/en/; every other interface language reads the English pages. Only plain file names inside
those two folders are served (no sub-folders, no "..").
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

TEN_HOP_LE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]*\.md$")
MUC_TOC = re.compile(r"^##\s+(Mục lục|Table of contents)\s*$", re.IGNORECASE)
DONG_MUC = re.compile(r"^(?P<lui>\s*)-\s+\[(?P<ten>[^\]]+)\]\((?P<file>[^)#\s]+)(?:#[^)]*)?\)\s*(?:-\s*(?P<mo_ta>.*))?$")
LANG_LINE = re.compile(r"^\*{1,3}\s*\[?\*{0,2}(Tiếng Việt|English)\b.*·", re.IGNORECASE)
CO_TOI_DA = 512 * 1024      # a guide page is a few dozen kB; anything bigger is not one of ours
KET_QUA_TOI_DA = 20


@dataclass
class GuideDeps:
    docs_dir: Callable[[], Path]


_DEPS: Optional[GuideDeps] = None


def lang_cua(lang: str) -> str:
    """Pages exist in Vietnamese and English only: vi reads docs/, every other language reads docs/en/."""
    return "vi" if str(lang or "").lower().startswith("vi") else "en"


def thu_muc(docs: Path, lang: str) -> Path:
    return docs if lang_cua(lang) == "vi" else docs / "en"


def doc_file(docs: Path, lang: str, name: str) -> Optional[Path]:
    """The page file, or None when the name is not a plain .md name in that folder."""
    name = str(name or "").strip()
    if not TEN_HOP_LE.match(name):
        return None
    goc = thu_muc(docs, lang).resolve()
    p = (goc / name).resolve()
    if p.parent != goc or not p.is_file():
        return None
    return p


def doc_text(p: Path) -> str:
    with p.open("rb") as f:
        return f.read(CO_TOI_DA).decode("utf-8", errors="replace")


def parse_index(md: str) -> List[Dict]:
    """Sections of the table of contents: [{title, items: [{name, title, desc, children: [...]}]}].

    Reads only the "## Mục lục" / "## Table of contents" block, and only links to pages in the same
    folder (a link that leaves the folder, like ../README.md, is not a guide page)."""
    sections: List[Dict] = []
    trong_toc = False
    for dong in md.splitlines():
        if dong.startswith("## "):
            trong_toc = bool(MUC_TOC.match(dong.strip()))
            continue
        if not trong_toc:
            continue
        if dong.startswith("### "):
            sections.append({"title": dong[4:].strip(), "items": []})
            continue
        m = DONG_MUC.match(dong)
        if not m or not sections or not TEN_HOP_LE.match(m.group("file")):
            continue
        muc = {"name": m.group("file"), "title": m.group("ten").strip(),
               "desc": (m.group("mo_ta") or "").strip(), "children": []}
        items = sections[-1]["items"]
        if m.group("lui") and items:
            items[-1]["children"].append(muc)
        else:
            items.append(muc)
    return [s for s in sections if s["items"]]


def bo_dau(s: str) -> str:
    """Lower case without Vietnamese marks, so "dinh ky" finds "định kỳ"."""
    s = unicodedata.normalize("NFD", str(s or "").lower()).replace("đ", "d")
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _tieu_de(md: str, name: str) -> str:
    for dong in md.splitlines():
        if dong.startswith("# "):
            return dong[2:].strip()
    return name


def _sach(dong: str) -> str:
    """One line of markdown as plain text for a search snippet."""
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", dong)
    s = re.sub(r"[`*_>#|]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def search(docs: Path, lang: str, q: str) -> List[Dict]:
    """Pages that contain every word of q (marks ignored), title matches first, then by number of hits."""
    tu = [w for w in bo_dau(q).split() if w]
    if not tu:
        return []
    goc = thu_muc(docs, lang)
    ra = []
    for p in sorted(goc.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        try:
            md = doc_text(p)
        except OSError:
            continue
        khong_dau = bo_dau(md)
        if not all(w in khong_dau for w in tu):
            continue
        tieu_de = _tieu_de(md, p.name)
        doan_du, doan_mot, du, mot = "", "", 0, 0
        for dong in md.splitlines():
            # Headings and the language switch line under the title ("*Tiếng Việt · [English](en/..)*") are not text.
            if dong.startswith("#") or LANG_LINE.match(dong):
                continue
            d = bo_dau(dong)
            n = sum(1 for w in tu if w in d)
            if n == len(tu):
                du += 1
                doan_du = doan_du or _sach(dong)[:180]
            elif n:
                mot += 1
                doan_mot = doan_mot or _sach(dong)[:180]
        tieu = re.findall(r"\w+", bo_dau(tieu_de))
        # A word in the title outweighs a long page that only mentions it often; a whole word ("telegram") counts more
        # than the start of one ("chat" in "chatbot").
        diem = sum(40 if w in tieu else (15 if any(x.startswith(w) for x in tieu) else 0) for w in tu) + 5 * du + mot
        ra.append({"name": p.name, "title": tieu_de, "snippet": doan_du or doan_mot, "score": diem})
    ra.sort(key=lambda x: (-x["score"], x["name"]))
    return ra[:KET_QUA_TOI_DA]


def _make_router() -> APIRouter:
    router = APIRouter()

    @router.get("/guide/index")
    async def guide_index(lang: str = Query("vi")):
        docs = _DEPS.docs_dir()
        p = doc_file(docs, lang, "README.md")
        if p is None:
            return JSONResponse({"ok": False, "error": "no_docs"}, status_code=404)
        md = await run_in_threadpool(doc_text, p)
        return {"ok": True, "lang": lang_cua(lang), "sections": parse_index(md)}

    @router.get("/guide/doc")
    async def guide_doc(name: str = Query(""), lang: str = Query("vi")):
        p = doc_file(_DEPS.docs_dir(), lang, name)
        if p is None:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        md = await run_in_threadpool(doc_text, p)
        return {"ok": True, "lang": lang_cua(lang), "name": p.name, "markdown": md}

    @router.get("/guide/search")
    async def guide_search(q: str = Query(""), lang: str = Query("vi")):
        kq = await run_in_threadpool(search, _DEPS.docs_dir(), lang, q[:200])
        return {"ok": True, "lang": lang_cua(lang), "results": kq}

    return router


def register(app, deps: GuideDeps) -> None:
    global _DEPS
    _DEPS = deps
    app.include_router(_make_router())
