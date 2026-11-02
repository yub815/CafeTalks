"""웹사이트용 데이터 만들기 (GitHub Actions가 매일 실행).

1) collector.py로 수집 → data/items.db (저장소에 같이 저장돼서 일주일치가 쌓임)
2) 최근 N일 자료를 docs/data/items.json 으로 내보내고, 이미지는 docs/img/ 에 저장
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import collector
import llm_dedupe
from app import clean_heading, default_comment, fetch_image

BASE = Path(__file__).resolve().parent
DOCS = BASE / "docs"
IMG = DOCS / "img"
KEEP_DAYS = 14
MAX_IMG_BYTES = 8 * 1024 * 1024

try:
    from PIL import Image
except ImportError:  # Pillow가 없으면 원본 그대로 저장
    Image = None


def save_image(url: str) -> str | None:
    name = hashlib.sha1(url.encode()).hexdigest()[:16]
    for ext in (".jpg", ".png", ".gif", ".webp", ".mp4"):
        if (IMG / f"{name}{ext}").exists():
            return f"img/{name}{ext}"
    got = fetch_image(url)
    if not got:
        return None
    data, ctype = got
    if len(data) > MAX_IMG_BYTES:
        return None
    if ctype in ("image/gif", "video/mp4") or Image is None:
        ext = {"image/gif": ".gif", "video/mp4": ".mp4", "image/png": ".png", "image/webp": ".webp"}.get(ctype, ".jpg")
        (IMG / f"{name}{ext}").write_bytes(data)
        return f"img/{name}{ext}"
    try:
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        if im.width > 900:
            im = im.resize((900, round(im.height * 900 / im.width)))
        im.save(IMG / f"{name}.jpg", "JPEG", quality=85)
        return f"img/{name}.jpg"
    except Exception:
        return None


def export() -> None:
    cfg = collector.load_config()
    IMG.mkdir(parents=True, exist_ok=True)
    since = (datetime.now(collector.KST) - timedelta(days=cfg.get("days", 7))).isoformat()
    con = collector.db()
    rows = con.execute("SELECT * FROM items WHERE posted_at>=? ORDER BY score DESC", (since,)).fetchall()
    bad = cfg.get("humor_exclude_words", [])
    out, keep = [], set()
    for r in rows:
        if r["category"] == "유머" and collector.is_controversial(r["title"], bad):
            continue
        imgs = []
        for u in json.loads(r["images"] or "[]")[:6]:
            p = save_image(u)
            if p:
                imgs.append(p)
                keep.add(Path(p).name)
        if not imgs:
            continue
        game = r["source"].startswith(("스팀", "itch"))
        out.append({
            "id": hashlib.sha1(r["url"].encode()).hexdigest()[:12],
            "category": r["category"], "source": r["source"], "title": r["title"], "url": r["url"],
            "views": r["views"], "likes": r["likes"], "comments": r["comments"], "score": r["score"],
            "posted_at": r["posted_at"],
            "heading": r["title"] if game else clean_heading(r["title"]),
            "comment": default_comment(r["summary"]),
            "images": imgs,
            "summary": r["summary"],
        })
    con.close()
    # Claude API로 같은 소식 묶기 (ANTHROPIC_API_KEY가 있을 때만)
    merged = []
    for cat in list(cfg.get("categories", {}).keys()) + ["유머"]:
        merged += llm_dedupe.dedupe(cat, [it for it in out if it["category"] == cat])
    out = merged
    for it in out:
        it.pop("summary", None)
    cats = {k: {"title": v.get("title", ""), "intro": v.get("intro", ""), "style": v.get("style", "block")}
            for k, v in cfg.get("categories", {}).items()}
    (DOCS / "data").mkdir(exist_ok=True)
    (DOCS / "data" / "items.json").write_text(json.dumps({
        "updated": datetime.now(collector.KST).isoformat(timespec="minutes"),
        "categories": cats, "items": out}, ensure_ascii=False), encoding="utf-8")
    # 오래된 이미지 정리
    cutoff = datetime.now().timestamp() - KEEP_DAYS * 86400
    for f in IMG.iterdir():
        if f.name not in keep and f.stat().st_mtime < cutoff:
            f.unlink()
    print(f"내보내기 완료: {len(out)}개")


if __name__ == "__main__":
    if "--export-only" not in sys.argv:
        collector.main()
    export()
