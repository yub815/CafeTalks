"""웹사이트용 데이터 만들기 (GitHub Actions가 매일 실행).

1) collector.py로 수집 → data/items.db (저장소에 같이 저장돼서 일주일치가 쌓임)
2) 최근 N일 자료를 docs/data/items.json 으로 내보내고, 이미지는 docs/img/ 에 저장
"""
from __future__ import annotations

import hashlib
import io
import json
import re
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


def check_product_images(out: list[dict], cfg: dict) -> list[dict]:
    """신상: 기사 본문 사진까지 모은 뒤 Claude가 제품이 보이는 사진만 남김. 맞는 사진이 없으면 항목을 뺌.
    결과는 data/imgcheck.json에 저장해서 다음 실행 때 다시 묻지 않음."""
    cats = cfg.get("photo_check_categories", ["신상"])
    path = BASE / "data" / "imgcheck.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    todo = [it for it in out if it["category"] in cats and it["url"] not in cache]
    for it in todo:
        cands = list(it["images"])
        for u in collector.article_images(it["url"]):
            p = save_image(u)
            if p and p not in cands and not p.endswith((".gif", ".mp4")):
                cands.append(p)
        it["images"] = cands[:6]
    for cat in cats:
        group = [it for it in todo if it["category"] == cat]
        picked = llm_dedupe.pick_product_images(cat, [
            {"title": it["title"], "paths": [str(DOCS / p) for p in it["images"]]} for it in group])
        for i, keep in picked.items():
            cache[group[i]["url"]] = [group[i]["images"][k] for k in keep]
    result = []
    for it in out:
        if it["category"] in cats and it["url"] in cache:
            it["images"] = [p for p in cache[it["url"]] if (DOCS / p).exists()]
            if not it["images"]:
                print(f"  - 제품 사진이 없어서 뺌: {it['title'][:40]}")
                continue
        result.append(it)
    keep_urls = {it["url"] for it in out}
    path.write_text(json.dumps({u: v for u, v in cache.items() if u in keep_urls}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return result


def backfill_humor_bodies() -> None:
    """예전에 본문 없이 저장된 유머 글은 원글 본문만 다시 읽어서 채움 (전체 재수집 아님)."""
    con = collector.db()
    rows = con.execute("SELECT url, source FROM items WHERE category='유머' AND (summary IS NULL OR summary='')").fetchall()
    n = 0
    for r in rows:
        sel, ref = collector.IMAGE_SELECTORS.get(r["source"], ("body", None))
        _, text = collector.post_content(r["url"], sel, ref)
        con.execute("UPDATE items SET summary=? WHERE url=?", (text or " ", r["url"]))
        n += bool(text.strip())
    con.commit()
    con.close()
    if rows:
        print(f"  유머 본문 {n}/{len(rows)}개 채움")


def export() -> None:
    cfg = collector.load_config()
    IMG.mkdir(parents=True, exist_ok=True)
    backfill_humor_bodies()
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
            "category": r["category"], "source": r["source"], "url": r["url"],
            "title": re.sub(r"^\d{1,2}\s+", "", r["title"]) if r["source"].startswith("루리웹") else r["title"],
            "views": r["views"], "likes": r["likes"], "comments": r["comments"], "score": r["score"],
            "posted_at": r["posted_at"],
            "heading": r["title"] if game else clean_heading(r["title"]),
            "comment": (r["summary"] or "").strip() if r["category"] == "유머" else default_comment(r["summary"]),
            "images": imgs,
            "summary": r["summary"],
        })
    con.close()
    # Claude API로 같은 소식 묶기 (ANTHROPIC_API_KEY가 있을 때만)
    merged = []
    for cat in list(cfg.get("categories", {}).keys()) + ["유머"]:
        merged += llm_dedupe.dedupe(cat, [it for it in out if it["category"] == cat])
    out = merged
    out = check_product_images(out, cfg)
    # Claude API로 카페 말투 제목줄·코멘트 (한 번 쓴 건 data/comments.json에 저장해서 재사용)
    cache_path = BASE / "data" / "comments.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    for cat, conf in cfg.get("categories", {}).items():
        todo = [it for it in out if it["category"] == cat and it["url"] not in cache]
        written = llm_dedupe.write_comments(cat, conf.get("style", "block"), todo)
        for i, w in written.items():
            cache[todo[i]["url"]] = w
    for it in out:
        w = cache.get(it["url"])
        if w and it["category"] != "유머":
            it["heading"] = w["heading"] or it["heading"]
            it["comment"] = w["comment"]
    keep_urls = {it["url"] for it in out if it["category"] != "유머"}
    cache = {u: w for u, w in cache.items() if u in keep_urls}
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    for it in out:
        it.pop("summary", None)
    cats = {k: {"title": v.get("title", ""), "intro": v.get("intro", ""), "style": v.get("style", "block")}
            for k, v in cfg.get("categories", {}).items()}
    (DOCS / "data").mkdir(exist_ok=True)
    (DOCS / "data" / "items.json").write_text(json.dumps({
        "updated": datetime.now(collector.KST).isoformat(timespec="minutes"),
        "categories": cats, "items": out}, ensure_ascii=False), encoding="utf-8")
    # 오래된 이미지 정리
    keep |= {Path(p).name for it in out for p in it["images"]}
    cutoff = datetime.now().timestamp() - KEEP_DAYS * 86400
    for f in IMG.iterdir():
        if f.name not in keep and f.stat().st_mtime < cutoff:
            f.unlink()
    print(f"내보내기 완료: {len(out)}개")


if __name__ == "__main__":
    if "--export-only" not in sys.argv:
        collector.main()
    export()
