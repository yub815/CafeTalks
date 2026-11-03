"""사이트의 '게임 검색'용 목록 만들기.

브라우저에서는 스팀·itch.io를 바로 검색할 수 없어서(사이트들이 막음), 매일 수집할 때
인기 게임 목록을 미리 모아 docs/data/games.json 에 넣어 둡니다. 사이트는 이 안에서 검색해요.

- 스팀: SteamSpy 인기 순위(최근 2주, 역대, 보유자 순) → 한국어 이름·설명·스크린샷 (하루 최대 MAX_DETAILS개씩 채움)
- itch.io: 웹게임 인기·평점 순위
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from bs4 import BeautifulSoup

import collector

BASE = Path(__file__).resolve().parent
CACHE = BASE / "data" / "games_index.json"
OUT = BASE / "docs" / "data" / "games.json"
MAX_DETAILS = 250          # 한 번 실행할 때 새로 읽는 스팀 게임 수 (스팀 요청 제한 때문에)
REFRESH_DAYS = 30          # 이만큼 지나면 설명·스크린샷 다시 읽기


def steamspy_ranking() -> list[tuple[str, str]]:
    """(appid, 영어 이름) 인기 순서대로."""
    out: list[tuple[str, str]] = []
    seen = set()
    for req in ("top100in2weeks", "top100forever", "top100owned", "all&page=0", "all&page=1"):
        r = collector.get(f"https://steamspy.com/api.php?request={req}")
        try:
            data = r.json() if r else {}
        except ValueError:
            data = {}
        rows = sorted(data.values(), key=lambda g: -(g.get("ccu") or 0)) if req.startswith("all") else data.values()
        for g in rows:
            appid = str(g.get("appid", ""))
            if appid and appid not in seen:
                seen.add(appid)
                out.append((appid, g.get("name", "")))
        time.sleep(1)
    return out


def steam_full(appid: str) -> dict | None:
    r = collector.get(f"https://store.steampowered.com/api/appdetails?appids={appid}&l=koreana&cc=kr")
    try:
        body = r.json()[appid] if r else None
    except (ValueError, KeyError):
        return None
    if not body or not body.get("success"):
        return {"skip": True}
    d = body["data"]
    if d.get("type") != "game":
        return {"skip": True}
    shots = [x.get("path_full") for x in d.get("screenshots", [])[:4] if x.get("path_full")]
    return {
        "ko": d.get("name", ""),
        "desc": BeautifulSoup(d.get("short_description", ""), "html.parser").get_text(" ", strip=True),
        "images": shots or ([d["header_image"]] if d.get("header_image") else []),
        "genres": [g.get("description", "") for g in d.get("genres", [])][:3],
    }


def itch_games() -> list[dict]:
    out = []
    for url in ("https://itch.io/games/platform-web", "https://itch.io/games/top-rated/platform-web",
                "https://itch.io/games/top-sellers/platform-web", "https://itch.io/games/new-and-popular/platform-web"):
        for page in (1, 2):
            r = collector.get(f"{url}?page={page}")
            if not r:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            for c in soup.select("div.game_cell"):
                a = c.select_one("a.title")
                img = c.select_one("img")
                src = (img.get("data-lazy_src") or img.get("src")) if img else None
                if not a or not a.get("href"):
                    continue
                txt = c.select_one(".game_text")
                out.append({"id": "itch:" + a["href"], "en": a.get_text(strip=True), "ko": "",
                            "url": a["href"], "desc": txt.get_text(" ", strip=True) if txt else "",
                            "images": [src] if src else [], "src": "itch.io 웹게임"})
            time.sleep(1)
    return out


def build(max_details: int = MAX_DETAILS) -> None:
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    now = time.time()
    ranking = steamspy_ranking()
    print(f"  게임 검색 목록: 스팀 인기 {len(ranking)}개 확인 중")
    fetched = 0
    for rank, (appid, en) in enumerate(ranking):
        g = cache.get(appid, {})
        g.update({"en": en or g.get("en", ""), "rank": rank})
        cache[appid] = g
        if fetched >= max_details or (g.get("t") and now - g["t"] < REFRESH_DAYS * 86400):
            continue
        info = steam_full(appid)
        if info is None:          # 접속 실패·요청 제한 → 다음에 다시
            time.sleep(5)
            continue
        g.update(info)
        g["t"] = now
        fetched += 1
        time.sleep(1.5)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    games = []
    for appid, g in sorted(cache.items(), key=lambda kv: kv[1].get("rank", 99999)):
        if g.get("skip") or not (g.get("en") or g.get("ko")):
            continue
        games.append({"id": "steam:" + appid, "en": g.get("en", ""), "ko": g.get("ko", ""),
                      "url": f"https://store.steampowered.com/app/{appid}/",
                      "desc": g.get("desc", ""), "genres": g.get("genres", []),
                      "images": g.get("images") or [f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg"],
                      "src": "스팀"})
    itch = itch_games()
    seen = {g["url"] for g in games}
    games += [g for g in itch if g["url"] not in seen and not seen.add(g["url"])]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(games, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"  게임 검색 목록: 스팀 {len(games) - len(itch)}개(이번에 {fetched}개 새로 읽음) + itch.io {len(itch)}개")


if __name__ == "__main__":
    build()
