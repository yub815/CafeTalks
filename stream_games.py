"""유튜브·방송(치지직)에서 요즘 유행하는 게임 찾기.

유튜브: 매일 '종합게임', '종겜' 같은 검색어로 최근 일주일 영상(조회수 순)을 모아 data/streams.json 에 넣고,
제목에서 게임 이름을 뽑아 주마다 조회수·채널 수로 순위를 매겨요 (유튜브가 주 기준, 치지직은 보너스 점수).

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


def snapshot(cfg: dict, with_youtube: bool = False) -> None:
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
    if with_youtube:
        rows += youtube(sc.get("youtube_queries", ["종합게임", "종겜"]))
    try:
        old = json.loads(SNAP.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        old = []
    cutoff = (datetime.now(collector.KST) - timedelta(weeks=cfg.get("keep_weeks", 5))).isoformat()
    ids = {r["id"] for r in rows if r["kind"] == "yt"}
    old = [r for r in old if r["t"] >= cutoff and r["kind"] != "cat" and r.get("id") not in ids] + rows
    SNAP.write_text(json.dumps(old, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  [게임 추천] 치지직 방송 {sum(r['kind'] == 'title' for r in rows)}개, "
          f"유튜브 영상 {sum(r['kind'] == 'yt' for r in rows)}개 저장")


def _ago(text: str) -> datetime:
    now = datetime.now(collector.KST)
    m = re.search(r"(\d+)\s*(초|분|시간|일|주|개월|년)", text or "")
    if not m:
        return now
    n = int(m[1])
    return now - {"초": timedelta(seconds=n), "분": timedelta(minutes=n), "시간": timedelta(hours=n),
                  "일": timedelta(days=n), "주": timedelta(weeks=n), "개월": timedelta(days=30 * n),
                  "년": timedelta(days=365 * n)}[m[2]]


def _videos(o):
    if isinstance(o, dict):
        if "videoRenderer" in o:
            yield o["videoRenderer"]
        for v in o.values():
            yield from _videos(v)
    elif isinstance(o, list):
        for v in o:
            yield from _videos(v)


def youtube(queries: list[str]) -> list[dict]:
    """유튜브 검색 (이번 주 업로드만, 조회수 순 + 관련도 순)."""
    out, seen = [], set()
    for q in queries:
        for sp in ("CAMSAggD", "EgIIAw%3D%3D"):  # 이번 주·조회수 순 / 이번 주
            r = collector.get(f"https://www.youtube.com/results?search_query={quote(q)}&sp={sp}&gl=KR&hl=ko",
                              headers={"Accept-Language": "ko-KR,ko"})
            m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text) if r else None
            if not m:
                continue
            try:
                vids = list(_videos(json.loads(m[1])))
            except json.JSONDecodeError:
                continue
            for v in vids:
                vid = v.get("videoId")
                when = (v.get("publishedTimeText") or {}).get("simpleText", "")
                if not vid or vid in seen or re.search(r"(주|개월|년)", when):
                    continue  # 일주일 넘은 영상은 뺌
                seen.add(vid)
                views = collector.to_int((v.get("viewCountText") or {}).get("simpleText", "0"))
                out.append({"t": _ago(when).isoformat(timespec="minutes"), "kind": "yt", "id": vid,
                            "title": "".join(x.get("text", "") for x in v.get("title", {}).get("runs", []))[:150],
                            "tags": [], "ch": ((v.get("ownerText") or {}).get("runs") or [{}])[0].get("text", ""),
                            "v": views, "img": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
            time.sleep(1)
    return out


NAME_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "name": {"type": "string"},
                       "en": {"type": "string", "description": "스팀 등에 올라간 영어 원래 이름 (알면). 모르면 빈 문자열"}},
        "required": ["i", "name", "en"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def _extract(titles: list[tuple[str, list[str]]]) -> dict[int, str]:
    """방송 제목들 → {번호: 하고 있는 게임/테스트 이름 (없으면 '')}."""
    import anthropic

    lines = [f"{i} | {t} | 태그: {', '.join(tags)}" for i, (t, tags) in enumerate(titles)]
    prompt = (
        "아래는 유튜브 영상과 치지직 방송의 제목·태그예요. 각 영상/방송에서 하고 있는 게임(스팀겜, 웹게임, 모바일겜)이나 "
        "인터넷 테스트(예: 에이징커브 테스트)의 이름을 name에 적어 주세요. 시청자가 직접 해볼 수 있는 게임을 추천하려는 거예요.\n"
        "- 제목이나 태그에 이름이 분명히 있을 때만 적고, 모르겠으면 빈 문자열.\n"
        "- 잡담·노래·합방·숙제처럼 게임 이름이 없으면 빈 문자열.\n"
        "- 스트리머끼리 하는 서버·컨텐츠(예: 연잎마을, 마크 서버 컨텐츠, 시참, 대회, 멤버 모집)는 일반인이 할 수 없으니 빈 문자열.\n"
        "- 게임 리뷰·뉴스·순위 영상, 게임 만들기(개발) 영상도 빈 문자열.\n"
        "- 여러 게임이 나열돼 있으면 첫 번째 것만.\n"
        "- 이름은 한국에서 흔히 부르는 공식 이름으로 맞춰 주세요 (띄어쓰기·영어/한글 표기를 통일해서 같은 게임은 같은 이름이 되게). "
        "'1일차', '데모', '신작' 같은 말은 빼요.\n\n" + "\n".join(lines)
    )
    client = anthropic.Anthropic()
    try:
        response = __import__("llm_dedupe").create(client, "게임 이름 뽑기",
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
    # 값은 "한국어 이름\t영어 이름" (영어 이름은 스팀 찾을 때 씀)
    return {r["i"]: (r["name"].strip() + ("\t" + r["en"].strip() if r["name"].strip() and r.get("en", "").strip() else ""))
            for r in rows if isinstance(r.get("i"), int) and 0 <= r["i"] < len(titles)}


def name_titles(snaps: list[dict]) -> dict[str, str]:
    try:
        cache = json.loads(NAMES.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    if os.environ.get("ANTHROPIC_API_KEY") and "게임 추천" not in __import__("llm_dedupe").SKIP:
        todo, seen = [], set()
        for r in snaps:
            if r["kind"] in ("title", "yt") and r["title"] not in cache and r["title"] not in seen:
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
            print(f"  [게임 추천] Claude가 영상·방송 제목 {len(todo)}개에서 게임 이름을 뽑았어요")
    titles = {r["title"] for r in snaps if r["kind"] in ("title", "yt")}
    cache = {t: n for t, n in cache.items() if t in titles}
    NAMES.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    return cache


def _key(name: str) -> str:
    return re.sub(r"[\s:·\-_'\"!?.,()\[\]]", "", name).lower()


STEAM_CACHE = BASE / "data" / "steam_lookup.json"


def steam_lookup(name: str, cache: dict) -> dict | None:
    """게임 이름 → 스팀 정보 (검색 목록에 없으면 스팀 검색으로 찾음)."""
    k = _key(name)
    if len(k) < 2:
        return None
    if k in cache:
        return cache[k] or None
    try:
        games = json.loads((BASE / "docs" / "data" / "games.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        games = []
    for g in games:
        if g.get("src") == "스팀" and k in (_key(g.get("ko", "")), _key(g.get("en", ""))):
            cache[k] = {"url": g["url"], "desc": g.get("desc", ""), "images": g.get("images", [])[:2]}
            return cache[k]
    r = collector.get(f"https://store.steampowered.com/api/storesearch/?term={quote(name)}&l=koreana&cc=KR")
    found = None
    try:
        for x in (r.json().get("items") or [])[:3] if r else []:
            xk = _key(x.get("name", ""))
            if xk and (xk == k or (len(k) >= 4 and (k in xk or xk in k))):
                det = collector.steam_details(str(x["id"]))
                found = {"url": f"https://store.steampowered.com/app/{x['id']}/", "desc": det.get("summary", ""),
                         "images": det.get("images", [])[:2]}
                break
    except (ValueError, KeyError):
        pass
    time.sleep(0.5)
    if r is not None:  # 접속이 됐을 때만 결과 저장 (못 찾음도 저장)
        cache[k] = found or {}
    return found


def collect(cfg: dict, top: int = 25) -> list[dict]:
    """주마다 유튜브에서 많이 보인 게임 → 게임 추천 항목들 (치지직 방송 수는 보너스)."""
    import build_site

    try:
        snaps = json.loads(SNAP.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    sc = cfg.get("stream_games", {})
    names = name_titles(snaps)
    cat_name = sc.get("category", "게임 추천")
    skip = {_key(x) for x in sc.get("skip_categories", [])}
    min_views = sc.get("min_youtube_views", 5000)
    weeks: dict[tuple[str, str], dict] = {}
    for r in snaps:
        if r["kind"] not in ("title", "yt"):
            continue
        name, _, en = names.get(r["title"], "").partition("\t")
        if not name or _key(name) in skip:
            continue
        wk = build_site.week_of(r["t"])
        g = weeks.setdefault((wk, _key(name)), {"name": name, "en": en, "week": wk, "first": r["t"], "yt": {}, "ytch": set(),
                                                "ch": set(), "imgs": [], "titles": []})
        g["first"] = min(g["first"], r["t"])
        g["en"] = g["en"] or en
        if r["kind"] == "yt":
            g["yt"][r["id"]] = r
            g["ytch"].add(r["ch"])
        else:
            g["ch"].add(r["ch"])
    out = []
    for g in weeks.values():
        views = sum(v["v"] for v in g["yt"].values())
        # 유튜브에 나온 게임만 (조회수가 적당히 있거나, 여러 채널이 올렸을 때)
        if not g["yt"] or (views < min_views and len(g["ytch"]) < 2):
            continue
        g["score"] = round(math.sqrt(views) + 20 * len(g["ytch"]) + 5 * len(g["ch"]), 1)
        g["views"] = views
        out.append(g)
    try:
        steam_cache = json.loads(STEAM_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        steam_cache = {}
    items = []
    for wk in sorted({g["week"] for g in out}):
        for g in sorted([g for g in out if g["week"] == wk], key=lambda g: g["score"], reverse=True)[:top]:
            vids = sorted(g["yt"].values(), key=lambda v: -v["v"])
            st = steam_lookup(g["name"], steam_cache) or (steam_lookup(g["en"], steam_cache) if g["en"] else None)
            if st:
                url, desc, shots = st["url"], st.get("desc", ""), st.get("images", [])
            else:  # 웹게임·테스트는 정확한 주소를 몰라서 네이버 검색으로 (글 쓸 때 바꿔 주세요)
                url, desc, shots = "https://search.naver.com/search.naver?query=" + quote(g["name"]), "", []
            imgs = []
            # 사진: 스팀 스크린샷. 스팀에 없으면 유튜브가 영상 중간에서 뽑은 게임 화면(hq1~3, 썸네일 글씨 없음)
            frames = [f"https://i.ytimg.com/vi/{v['id']}/hq{n}.jpg" for v in vids[:2] for n in (2, 1, 3)]
            for u in (shots or frames):
                if len(imgs) >= 4:
                    break
                if u not in imgs and build_site.save_image(u):
                    imgs.append(u)
            if not imgs:
                continue
            items.append({
                "category": cat_name, "source": "유튜브·방송 인기",
                "url": url.split("#")[0] + "#w" + wk,  # 주마다 따로 (지난주에 떴던 게임이 이번 주에 또 떠도 보이게)
                "title": g["name"],
                "summary": (desc + "\n" if desc else "") + "요즘 유튜브·방송에서 많이 하는 게임. 영상 제목 예: "
                           + " / ".join(v["title"] for v in vids[:4]),
                "images": imgs, "views": g["views"], "score": g["score"] + 200,  # 스팀 순위보다 위에
                "posted_at": g["first"],
            })
    STEAM_CACHE.write_text(json.dumps(steam_cache, ensure_ascii=False), encoding="utf-8")
    print(f"  [게임 추천] 유튜브·방송에서 많이 보인 게임 {len(items)}개")
    return items
