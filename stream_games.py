"""방송(치지직)에서 요즘 유행하는 게임 찾기.

웹게임·테스트류(예: 에이징커브 테스트)는 따로 카테고리가 없어서 '종합 게임'이나 '토크'로 방송하고,
게임 이름은 방송 제목에 들어가요. 그래서 3시간마다 방송 목록을 찍어 두고(data/streams.json),
Claude(Haiku)가 제목에서 게임 이름을 뽑아 주마다 방송한 채널 수·시청자 수로 순위를 매겨요.
치지직에 따로 카테고리가 있는 게임은 카테고리 이름을 그대로 씁니다 (늘 하는 큰 게임은 config에서 뺌).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import collector

BASE = Path(__file__).resolve().parent
SNAP = BASE / "data" / "streams.json"
NAMES = BASE / "data" / "stream_names.json"
API = "https://api.chzzk.naver.com/service"
GAMEISH = re.compile(r"테스트|게임|겜|검사|플레이|공포|모드|체험|데모")


def _json(url: str) -> dict | None:
    r = collector.get(url, headers={"Accept": "application/json"})
    try:
        return r.json().get("content") if r else None
    except ValueError:
        return None


def _lives(ctype: str, cid: str, max_pages: int) -> list[dict]:
    out, nxt = [], None
    for _ in range(max_pages):
        q = "size=50"
        if nxt:
            q += "&" + "&".join(f"{k}={quote(str(v))}" for k, v in nxt.items())
        c = _json(f"{API}/v2/categories/{ctype}/{cid}/lives?{q}")
        if not c:
            break
        out += c.get("data", [])
        nxt = (c.get("page") or {}).get("next")
        if not nxt or (out and out[-1].get("concurrentUserCount", 0) < 2):
            break
        time.sleep(0.5)
    return out


def _thumb(live: dict) -> str:
    u = live.get("liveImageUrl") or ""
    return u.replace("{type}", "720") if u else ""


def snapshot(cfg: dict) -> None:
    """지금 치지직 방송 목록을 data/streams.json 에 추가."""
    sc = cfg.get("stream_games", {})
    if not sc.get("enabled", True):
        return
    now = datetime.now(collector.KST).isoformat(timespec="minutes")
    rows: list[dict] = []
    # 1) 종합 게임 · 토크 방송 제목 (게임 이름은 Claude가 나중에 뽑음)
    for ctype, cid, pages, need_kw in (("GAME", "various_games", 6, False), ("ETC", "talk", 14, True)):
        for l in _lives(ctype, cid, pages):
            title, tags = l.get("liveTitle") or "", l.get("tags") or []
            if need_kw and not GAMEISH.search(title + " " + " ".join(tags)):
                continue
            rows.append({"t": now, "kind": "title", "title": title[:120], "tags": tags[:6],
                         "ch": (l.get("channel") or {}).get("channelId", ""),
                         "v": l.get("concurrentUserCount", 0), "img": _thumb(l)})
    # 2) 따로 카테고리가 있는 게임
    skip = set(sc.get("skip_categories", []))
    cats, nxt = [], None
    for _ in range(4):
        q = "size=50" + ("&" + "&".join(f"{k}={quote(str(v))}" for k, v in nxt.items()) if nxt else "")
        c = _json(f"{API}/v1/categories/live?{q}")
        if not c:
            break
        cats += c.get("data", [])
        nxt = (c.get("page") or {}).get("next")
        if not nxt:
            break
    for c in cats:
        name = c.get("categoryValue", "")
        if c.get("categoryType") != "GAME" or c.get("categoryId") == "various_games" or name in skip:
            continue
        if c.get("openLiveCount", 0) < sc.get("min_category_channels", 5):
            continue
        rows.append({"t": now, "kind": "cat", "name": name, "cid": c.get("categoryId"),
                     "n": c.get("openLiveCount", 0), "v": c.get("concurrentUserCount", 0),
                     "new": bool(c.get("newCategory")), "img": c.get("posterImageUrl") or ""})
    try:
        old = json.loads(SNAP.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        old = []
    cutoff = (datetime.now(collector.KST) - timedelta(weeks=cfg.get("keep_weeks", 5))).isoformat()
    old = [r for r in old if r["t"] >= cutoff] + rows
    SNAP.write_text(json.dumps(old, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  [게임 추천] 치지직 방송 {sum(r['kind'] == 'title' for r in rows)}개, "
          f"게임 카테고리 {sum(r['kind'] == 'cat' for r in rows)}개 저장")


NAME_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "name": {"type": "string"}},
        "required": ["i", "name"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def _extract(titles: list[tuple[str, list[str]]]) -> dict[int, str]:
    """방송 제목들 → {번호: 하고 있는 게임/테스트 이름 (없으면 '')}."""
    import anthropic

    lines = [f"{i} | {t} | 태그: {', '.join(tags)}" for i, (t, tags) in enumerate(titles)]
    prompt = (
        "아래는 치지직 '종합 게임'·'토크' 방송 제목과 태그예요. 각 방송에서 지금 하고 있는 게임(스팀겜, 웹게임, 모바일겜)이나 "
        "인터넷 테스트(예: 에이징커브 테스트)의 이름을 name에 적어 주세요.\n"
        "- 제목이나 태그에 이름이 분명히 있을 때만 적고, 모르겠으면 빈 문자열.\n"
        "- 잡담·노래·합방·숙제처럼 게임 이름이 없으면 빈 문자열.\n"
        "- 여러 게임이 나열돼 있으면 첫 번째 것만.\n"
        "- 이름은 한국에서 흔히 부르는 공식 이름으로 맞춰 주세요 (띄어쓰기·영어/한글 표기를 통일해서 같은 게임은 같은 이름이 되게). "
        "'1일차', '데모', '신작' 같은 말은 빼요.\n\n" + "\n".join(lines)
    )
    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model="claude-haiku-5-5", max_tokens=16000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": NAME_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        print(f"  ! Claude API 오류({str(getattr(e, 'message', e))[:200]}). 게임 이름 뽑기를 건너뜁니다.")
        return {}
    if response.stop_reason in ("refusal", "max_tokens"):
        return {}
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        rows = json.loads(text).get("items", [])
    except (json.JSONDecodeError, AttributeError):
        return {}
    return {r["i"]: r["name"].strip() for r in rows if isinstance(r.get("i"), int) and 0 <= r["i"] < len(titles)}


def name_titles(snaps: list[dict]) -> dict[str, str]:
    try:
        cache = json.loads(NAMES.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    if os.environ.get("ANTHROPIC_API_KEY") and "게임 추천" not in __import__("llm_dedupe").SKIP:
        todo, seen = [], set()
        for r in snaps:
            if r["kind"] == "title" and r["title"] not in cache and r["title"] not in seen:
                seen.add(r["title"])
                todo.append((r["title"], r.get("tags", [])))
        for s in range(0, len(todo), 150):
            part = todo[s:s + 150]
            got = _extract(part)
            if not got:
                break
            for i, (t, _) in enumerate(part):
                cache[t] = got.get(i, "")
        if todo:
            print(f"  [게임 추천] Claude가 방송 제목 {len(todo)}개에서 게임 이름을 뽑았어요")
    titles = {r["title"] for r in snaps if r["kind"] == "title"}
    cache = {t: n for t, n in cache.items() if t in titles}
    NAMES.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    return cache


def _key(name: str) -> str:
    return re.sub(r"[\s:·\-_'\"!?.,()\[\]]", "", name).lower()


def steam_lookup(name: str) -> dict | None:
    try:
        games = json.loads((BASE / "docs" / "data" / "games.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    k = _key(name)
    for g in games:
        if g.get("src") == "스팀" and len(k) >= 2 and k in (_key(g.get("ko", "")), _key(g.get("en", ""))):
            return g
    return None


def collect(cfg: dict, top: int = 25) -> list[dict]:
    """주마다 방송에서 많이 보인 게임 → 게임 추천 항목들."""
    import build_site  # 사진을 바로 내려받아 둠 (방송 썸네일은 금방 바뀜)

    try:
        snaps = json.loads(SNAP.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    names = name_titles(snaps)
    cat_name = cfg.get("stream_games", {}).get("category", "게임 추천")
    skip = {_key(s) for s in cfg.get("stream_games", {}).get("skip_categories", [])}
    weeks: dict[tuple[str, str], dict] = {}
    for r in snaps:
        wk = build_site.week_of(r["t"])
        if r["kind"] == "title":
            name = names.get(r["title"], "")
            if not name or _key(name) in skip:
                continue
        else:
            name = r["name"]
        g = weeks.setdefault((wk, _key(name)), {"name": name, "week": wk, "first": r["t"], "ch": {}, "cat": {}, "cid": "", "new": False,
                                                "imgs": [], "titles": []})
        g["first"] = min(g["first"], r["t"])
        if r["kind"] == "title":
            g["ch"][r["ch"]] = max(g["ch"].get(r["ch"], 0), r["v"])
            if r["title"] not in g["titles"]:
                g["titles"].append(r["title"])
            if r.get("img"):
                g["imgs"].append((r["v"], r["img"]))
        else:
            g["cat"][r["t"]] = (r["n"], r["v"])
            g["cid"], g["new"] = r.get("cid", ""), g["new"] or r.get("new", False)
            if r.get("img") and not any(u == r["img"] for _, u in g["imgs"]):
                g["imgs"].append((0, r["img"]))
    out = []
    for (wk, k), g in weeks.items():
        chans = len(g["ch"])
        view = sum(g["ch"].values())
        if g["cat"]:  # 카테고리 게임: 찍은 때마다의 평균 방송 수·시청자 수
            n = sum(a for a, _ in g["cat"].values()) / len(g["cat"])
            v = sum(b for _, b in g["cat"].values()) / len(g["cat"])
            # 늘 하는 게임은 빼고, 지난주들보다 시청자가 1.5배 넘게 늘었을 때만 (처음 보는 카테고리는 통과)
            # (기록이 아직 없으면 치지직이 '신규'로 표시한 카테고리만)
            prev = [x for (w2, k2), x in weeks.items() if k2 == k and w2 < wk and x["cat"]]
            if prev:
                pv = sum(sum(b for _, b in x["cat"].values()) / len(x["cat"]) for x in prev) / len(prev)
                if v < pv * 1.5:
                    continue
            elif not g["new"]:
                continue
            g["is_cat"] = True
            chans, view = max(chans, round(n)), max(view, v)
        if chans < 2:
            continue
        g["score"] = round(chans * 10 + math.sqrt(view), 1)
        g["chans"] = chans
        out.append(g)
    items = []
    for wk in sorted({g["week"] for g in out}):
        ranked = sorted([g for g in out if g["week"] == wk], key=lambda g: g["score"], reverse=True)
        cats = [g for g in ranked if g.get("is_cat")][:5]  # 카테고리 있는 큰 게임은 5개까지만
        best = sorted([g for g in ranked if not g.get("is_cat")][:top - len(cats)] + cats,
                      key=lambda g: g["score"], reverse=True)
        for g in best:
            st = steam_lookup(g["name"])
            if st:
                url, desc, shots = st["url"], st.get("desc", ""), st.get("images", [])[:2]
            elif g["cid"]:
                url, desc, shots = f"https://chzzk.naver.com/category/GAME/{g['cid']}", "", []
            else:  # 웹게임·테스트는 정확한 주소를 몰라서 네이버 검색으로 (글 쓸 때 바꿔 주세요)
                url, desc, shots = "https://search.naver.com/search.naver?query=" + quote(g["name"]), "", []
            # 방송 화면은 시청자 많은 순으로 2장 (금방 사라지니 바로 저장)
            live = [u for _, u in sorted(g["imgs"], key=lambda x: -x[0])]
            imgs = []
            for u in shots + live:
                if len(imgs) >= 4:
                    break
                if u not in imgs and build_site.save_image(u):
                    imgs.append(u)
            if not imgs:
                continue
            items.append({
                "category": cat_name, "source": "치지직 방송",
                "url": url.split("#")[0] + "#w" + wk,  # 주마다 따로 (지난주에 떴던 게임이 이번 주에 또 떠도 보이게)
                "title": g["name"],
                "summary": (desc + "\n" if desc else "") + "요즘 방송에서 많이 하는 게임."
                           + (" 방송 제목 예: " + " / ".join(g["titles"][:5]) if g["titles"] else ""),
                "images": imgs, "views": g["chans"], "score": g["score"] + 200,  # 스팀 순위보다 위에
                "posted_at": g["first"],
            })
    print(f"  [게임 추천] 방송에서 많이 보인 게임 {len(items)}개")
    return items
