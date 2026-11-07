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


def this_monday() -> datetime:
    now = datetime.now(collector.KST)
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def week_of(posted_at: str) -> str:
    """그 글이 속한 주의 월요일 날짜 (YYYY-MM-DD). 주차 묶음 기준."""
    d = datetime.fromisoformat(posted_at)
    if d.tzinfo is None:
        d = d.replace(tzinfo=collector.KST)
    d = d.astimezone(collector.KST)
    return (d - timedelta(days=d.weekday())).date().isoformat()


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
        cands = []
        src = [v for u in it["source_images"] for v in (collector.untumb(u), u) if v]
        for u in collector.article_images(it["url"]) + src:
            p = save_image(u)
            if p and p not in cands and not p.endswith((".gif", ".mp4")):
                cands.append(p)
        it["images"] = cands[:6] or it["images"]
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


def filter_humor(out: list[dict], cfg: dict) -> list[dict]:
    """유머 탭: 짤 1~2장짜리 짧은 짤만 (사진 수·본문 길이로 먼저 거르고),
    시사·정치·사건사고·성별 논쟁 같은 글은 Claude가 뺌 (결과는 data/humorcheck.json에 저장)."""
    max_imgs = cfg.get("humor_max_images", 2)
    out = [it for it in out if it["category"] != "유머"
           or (len(it["images"]) <= max_imgs and len(it.get("comment") or "") <= 200)]
    path = BASE / "data" / "humorcheck.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    todo = [it for it in out if it["category"] == "유머" and it["url"] not in cache]
    for start in range(0, len(todo), 40):
        part = todo[start:start + 40]
        res = llm_dedupe.humor_filter([{"title": it["title"], "body": it.get("comment", ""),
                                         "images": len(it["images"])} for it in part])
        for i, ok in res.items():
            cache[part[i]["url"]] = ok
    result = [it for it in out if not (it["category"] == "유머" and cache.get(it["url"]) is False)]
    urls = {it["url"] for it in out}
    path.write_text(json.dumps({u: v for u, v in cache.items() if u in urls}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return result


def same_item(a: dict, b: dict) -> bool:
    """같은 제품 소식인지 (브랜드만 같고 제품이 다른 건 따로 둠)."""
    ia, ib = (a["images"] or [None])[0], (b["images"] or [None])[0]
    if ia and ia == ib:
        return True
    qa = {q.replace(" ", "") for q in collector.quoted(a["title"])}
    qb = {q.replace(" ", "") for q in collector.quoted(b["title"])}
    if any(len(x) >= 3 and len(y) >= 3 and (x in y or y in x) for x in qa for y in qb):
        return True
    ta, tb = collector.tokens(a["title"]), collector.tokens(b["title"])
    if not ta or not tb:
        return False
    # 회사 이름은 빼고 겹치는 단어로 판단 (같은 회사의 다른 제품은 따로 둠)
    brand = {w.lower() for w in collector.brands(a["title"]) | collector.brands(b["title"])}
    common = {w for w in ta & tb if w not in brand and not any(w in x or x in w for x in brand)}
    long_ = [w for w in common if len(w) >= 3]
    if len(long_) >= 2 or (long_ and len(common) >= 2):
        return True
    return len(common) / min(len(ta), len(tb)) >= 0.6


def rule_dedupe(out: list[dict]) -> list[dict]:
    kept: list[dict] = []
    dropped = 0
    for it in out:
        if it["category"] == "유머" or it["source"].startswith(("스팀", "itch")):
            kept.append(it)
            continue
        if any(k["category"] == it["category"] and k["week"] == it["week"] and same_item(it, k) for k in kept):
            dropped += 1
            continue
        kept.append(it)
    if dropped:
        print(f"  규칙으로 중복 {dropped}개를 묶었어요")
    return kept


def export() -> None:
    cfg = collector.load_config()
    IMG.mkdir(parents=True, exist_ok=True)
    backfill_humor_bodies()
    since = (this_monday() - timedelta(weeks=cfg.get("keep_weeks", 5) - 1)).isoformat()
    con = collector.db()
    rows = con.execute("SELECT * FROM items WHERE posted_at>=? ORDER BY score DESC", (since,)).fetchall()
    bad = cfg.get("humor_exclude_words", [])
    out, keep = [], set()
    for r in rows:
        if r["category"] == "유머" and collector.is_controversial(r["title"], bad):
            continue
        cat_conf = cfg.get("categories", {}).get(r["category"], {})
        if any(w in r["title"] for w in cat_conf.get("exclude_words", [])):
            continue  # 나중에 추가한 제외 단어도 이미 모은 자료에 바로 적용
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
            "week": week_of(r["posted_at"]),
            "heading": r["title"] if game else clean_heading(r["title"]),
            "comment": (r["summary"] or "").strip() if r["category"] == "유머" else default_comment(r["summary"]),
            "images": imgs,
            "source_images": json.loads(r["images"] or "[]")[:6],
            "summary": r["summary"],
        })
    con.close()
    # 규칙으로 먼저 같은 소식 묶기 (같은 주 안에서, 날마다 따로 모인 기사끼리도) → Claude가 안 될 때도 중복이 덜 남게
    out = rule_dedupe(out)
    # Claude API로 같은 소식 묶기 (ANTHROPIC_API_KEY가 있을 때만)
    merged = []
    for cat in list(cfg.get("categories", {}).keys()) + ["유머"]:
        for wk in sorted({it["week"] for it in out if it["category"] == cat}):
            merged += llm_dedupe.dedupe(cat, [it for it in out if it["category"] == cat and it["week"] == wk])
    out = merged
    out = check_product_images(out, cfg)
    out = filter_humor(out, cfg)
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
        it.pop("source_images", None)
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
    if "--games-only" in sys.argv:  # 게임 검색 목록만 채우기
        import games_index
        games_index.build(max_details=800)
        sys.exit(0)
    if "--humor-only" in sys.argv:
        collector.main(humor_only=True)
    elif "--export-only" not in sys.argv:
        collector.main()
        try:
            import games_index
            games_index.build()
        except Exception as e:  # 검색 목록이 실패해도 나머지는 계속
            print(f"  ! 게임 검색 목록 만들기 실패: {e}")
    export()
