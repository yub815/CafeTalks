"""이번 주 방영한 애니 회차 중 IMDb 평점이 높은 회차 모으기.

- 무엇이 언제 방영됐는지: AniList (방영 일정, 인기도, 대표 사진)
- 회차 평점: IMDb가 매일 공개하는 데이터 파일 (datasets.imdbws.com, 사이트를 긁지 않음)
- 한국어 제목: AniList에 한글 제목이 있으면 그것, 없으면 Claude(Haiku)가 한국에서 부르는 이름으로 (캐시)
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import build_site
import collector

BASE = Path(__file__).parent
KO_CACHE = BASE / "data" / "anime_ko.json"
EXTRA = BASE / "data" / "anime_extra.json"  # 최고 평점 에피소드 TOP 3 (글 끝에 글자로)
IMDB = "https://datasets.imdbws.com/"

ANILIST_Q = """query($p:Int,$a:Int,$b:Int){Page(page:$p,perPage:50){pageInfo{hasNextPage}
airingSchedules(airingAt_greater:$a,airingAt_lesser:$b,sort:TIME){episode airingAt
media{id isAdult popularity countryOfOrigin format siteUrl synonyms bannerImage coverImage{extraLarge}
title{romaji english native}}}}}"""


def _norm(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"(season|part|cour)\s*\d+|\d+(st|nd|rd|th)\b(\s*(season|part|cour|stage))?|\bstage\b|第\s*\d+\s*(期|クール)|"
               r"\b(ii|iii|iv)\b|[:：\-–!?！？.,'\"()（）\[\]「」『』・~〜&＆]", " ", t)
    return re.sub(r"\s+", "", t)


def airings(days: int, min_pop: int) -> list[dict]:
    now = int(time.time())
    out, page = [], 1
    while page <= 40:
        r = requests.post("https://graphql.anilist.co", timeout=30, json={
            "query": ANILIST_Q, "variables": {"p": page, "a": now - days * 86400, "b": now}})
        if r.status_code == 429:
            time.sleep(60)
            continue
        r.raise_for_status()
        pg = r.json()["data"]["Page"]
        for a in pg["airingSchedules"]:
            m = a["media"]
            if (m["countryOfOrigin"] == "JP" and not m["isAdult"] and m["format"] in ("TV", "ONA", "TV_SHORT")
                    and (m["popularity"] or 0) >= min_pop):
                out.append(a)
        if not pg["pageInfo"]["hasNextPage"]:
            break
        page += 1
        time.sleep(0.8)
    return out


def _lines(name: str):
    with requests.get(IMDB + name, stream=True, timeout=120) as r:
        r.raise_for_status()
        with gzip.GzipFile(fileobj=r.raw) as gz:
            for line in io.TextIOWrapper(gz, encoding="utf-8"):
                yield line.rstrip("\n").split("\t")


def id_map() -> dict[int, list[str]]:
    """AniList 작품 번호 → IMDb 작품 번호 (커뮤니티가 관리하는 애니 ID 대조표, github.com/Fribb/anime-lists)."""
    try:
        r = requests.get("https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json", timeout=60)
        r.raise_for_status()
        out = {}
        for x in r.json():
            ids = x.get("imdb_id") or []
            ids = [ids] if isinstance(ids, str) else ids
            if x.get("anilist_id") and ids:
                out[int(x["anilist_id"])] = [i for i in ids if str(i).startswith("tt")]
        return out
    except Exception as e:
        print(f"  ! 애니 ID 대조표 실패: {e}")
        return {}


def imdb_index(wanted: set[str], year: int, wanted_ids: set[str] = frozenset()):
    """애니 시리즈(제목 → tconst, 'id:tt…' → tconst)와 최근 2년 회차(tconst → 년도)."""
    series: dict[str, set[str]] = {}
    eps: dict[str, int] = {}
    for c in _lines("title.basics.tsv.gz"):
        if len(c) < 9:
            continue
        if c[0] in wanted_ids and c[1] in ("tvSeries", "tvMiniSeries"):
            series.setdefault("id:" + c[0], set()).add(c[0])
        if c[1] in ("tvSeries", "tvMiniSeries") and "Animation" in c[8]:
            for t in {_norm(c[2]), _norm(c[3])}:
                if t in wanted:
                    series.setdefault(t, set()).add(c[0])
        elif c[1] == "tvEpisode" and c[5].isdigit() and int(c[5]) >= year - 1:
            eps[c[0]] = int(c[5])
    ids = set().union(*series.values()) if series else set()
    by_series: dict[str, list[tuple[int, int, str]]] = {}
    for c in _lines("title.episode.tsv.gz"):
        if len(c) == 4 and c[1] in ids and c[0] in eps and c[2].isdigit() and c[3].isdigit():
            by_series.setdefault(c[1], []).append((int(c[2]), int(c[3]), c[0]))
    need = {e for v in by_series.values() for _, _, e in v}
    ratings = {c[0]: (float(c[1]), int(c[2])) for c in _lines("title.ratings.tsv.gz")
               if c[0] in need and c[1] != "averageRating"}
    return series, by_series, eps, ratings


def korean_titles(media: list[dict]) -> dict[str, str]:
    try:
        cache = json.loads(KO_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    todo = []
    for m in media:
        k = str(m["id"])
        if k in cache:
            continue
        hangul = next((s for s in m.get("synonyms") or [] if re.search("[가-힣]", s)), None)
        if hangul:
            cache[k] = hangul
        else:
            todo.append(m)
    if todo and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        lines = [f"{m['id']} | {m['title']['native']} | {m['title']['romaji']} | {m['title'].get('english') or ''}"
                 for m in todo]
        schema = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {
            "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                      "required": ["id", "ko"], "properties": {"id": {"type": "integer"}, "ko": {"type": "string"}}}}}}
        try:
            resp = __import__("llm_dedupe").create(anthropic.Anthropic(), "애니 한국어 제목",
                model="claude-haiku-5-5", max_tokens=8000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content":
                    "아래 일본 애니들의 한국어 제목을 적어 주세요. 한국 정식 발매·방영 제목(라프텔·애니플러스 등)이 있으면 그것, "
                    "없으면 한국 팬들이 흔히 부르는 이름. 2기·3기는 '2기'처럼 붙이고, 같은 기의 2쿨은 '2쿨'로. "
                    "모르면 일본어 제목을 자연스럽게 번역해 주세요. 한자·가나는 남기지 말고 전부 한글로.\n\n" + "\n".join(lines)}])
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            for r in json.loads(text).get("items", []):
                if r.get("ko", "").strip():
                    cache[str(r["id"])] = r["ko"].strip()
        except Exception as e:  # 제목 번역이 안 돼도 영어 제목으로 진행
            print(f"  ! 애니 제목 번역 실패: {e}")
    KO_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return cache


def collect(cfg: dict) -> list[dict]:
    """지난달 방영한 메이저 애니: 회차 평점 TOP + 작품 종합(그 달 회차 평균) TOP. 한 달에 한 번 올리는 글용."""
    ac = cfg.get("anime_ratings", {})
    if not ac.get("enabled", True):
        return []
    now = datetime.now(collector.KST)
    start = (now.replace(day=1) - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = f"{start.year}-{start.month:02d}"
    fetched = airings((now - start).days + 1, ac.get("min_popularity", 30000))
    latest: dict[int, int] = {}  # 작품별 지금까지 방영된 마지막 화 (AniList 기준)
    for a in fetched:
        latest[a["media"]["id"]] = max(latest.get(a["media"]["id"], 0), a["episode"])
    air = [a for a in fetched if start.timestamp() <= a["airingAt"] < end.timestamp()]
    print(f"  [애니 평점] {month} 방영 기록 {len(air)}개")
    by_mid: dict[int, list[dict]] = {}
    for a in air:
        by_mid.setdefault(a["media"]["id"], []).append(a)
    idmap = id_map()
    exact, prefix = {}, {}
    for mid, lst in by_mid.items():
        m = lst[0]["media"]
        t = m["title"]
        all_t = [x for x in (t["romaji"], t.get("english"), t["native"], *(m["synonyms"] or [])) if x]
        exact[mid] = {_norm(x) for x in all_t} | {"id:" + i for i in idmap.get(mid, [])}
        # "JoJo's Bizarre Adventure: Steel Ball Run" 처럼 IMDb엔 앞부분 이름으로만 있는 경우 (정확한 이름이 없을 때만 씀)
        prefix[mid] = {_norm(x.split(":")[0]) for x in all_t if ":" in x and len(_norm(x.split(":")[0])) >= 8}
    wanted = set().union(*exact.values(), *prefix.values()) if exact else set()
    wanted_ids = {i for mid in by_mid for i in idmap.get(mid, [])}
    series, by_series, ep_year, ratings = imdb_index(wanted, now.year, wanted_ids)
    print(f"  [애니 평점] IMDb에서 찾은 애니 {len(series)}개 (ID 대조표 {len(wanted_ids)}개)")
    ko = korean_titles([lst[0]["media"] for lst in by_mid.values()])
    min_votes = ac.get("min_votes", 50)
    top = ac.get("top", 30)
    eps, by_media, notes = [], {}, {}

    def try_series(sid, mid, lst):
        """그 IMDb 작품에서 이번 달 방영분과 맞는 회차들. (맞은 회차 목록, 화수 차이)"""
        yr = datetime.fromtimestamp(lst[0]["airingAt"], collector.KST).year
        recent = [(se, epn, tc) for se, epn, tc in by_series.get(sid, []) if ep_year.get(tc) in (yr, yr - 1)]
        if not recent:
            return [], 0
        season = max(se for se, _, _ in recent)
        rated = [epn for se, epn, tc in recent if se == season and tc in ratings]
        # 2기·2쿨을 AniList는 1화부터, IMDb는 앞 시즌에 이어서 셀 때가 있어서 화수 차이를 맞춤
        # (IMDb에 평점 붙은 마지막 화가 AniList 최신화보다 크면 그만큼 밀려 있는 것)
        off = max(0, max(rated, default=0) - latest.get(mid, 0))
        idx = {epn: tc for se, epn, tc in recent if se == season and tc in ratings}
        return [(a, a["episode"] + off, idx[a["episode"] + off]) for a in lst if a["episode"] + off in idx], off

    for mid, lst in by_mid.items():
        m = lst[0]["media"]
        mapped = {"id:" + i for i in idmap.get(mid, [])}
        keys = exact[mid] if any(k in series for k in exact[mid]) else prefix[mid]
        tried = []
        for k in keys:
            for sid in series.get(k, ()):
                got, off = try_series(sid, mid, lst)
                if got:
                    tried.append((len(got), ("id:" + sid) in mapped, sid, got, off))
        if not tried:
            continue
        tried.sort(key=lambda x: (-x[0], not x[1]))  # 맞는 회차가 많은 쪽, 같으면 대조표에 있는 쪽
        _, in_map, sid, got, off = tried[0]
        note = []
        if keys is prefix[mid]:
            note.append("제목 일부로만 찾음")
        if mapped and not in_map:
            note.append("ID 대조표와 다른 IMDb 작품")
        if len({t[2] for t in tried if t[0] == tried[0][0]}) > 1:
            note.append("IMDb에 비슷한 작품이 여러 개")
        if off:
            note.append(f"IMDb가 화수를 이어서 셈 (방영 {lst[0]['episode']}화 = IMDb {lst[0]['episode'] + off}화)")
        title_ko = ko.get(str(mid)) or m["title"].get("english") or m["title"]["romaji"]
        imgs = [u for u in (m.get("bannerImage"), (m.get("coverImage") or {}).get("extraLarge")) if u]
        if not imgs:
            continue
        for a, n, tc in got:
            rating, votes = ratings[tc]
            if votes < min_votes:
                continue
            e = {"m": m, "n": n, "tc": tc, "sid": sid, "rating": rating, "votes": votes, "ko": title_ko, "imgs": imgs}
            eps.append(e)
            by_media.setdefault(mid, []).append(e)
        if note:
            notes[f"https://www.imdb.com/title/{sid}/#m{month}"] = " · ".join(note)
    cat = ac.get("category", "애니 평점")
    posted = now.isoformat()  # 이번 주 목록에 보이게 (매일 다시 계산)
    orig = lambda m: f"원제 {m['title']['native']} / {m['title'].get('english') or m['title']['romaji']}"
    items = []
    # 맨 끝에 글자로만 넣는 최고 평점 에피소드 TOP 3 (작품당 1회차)
    best, seen = [], set()
    for e in sorted(eps, key=lambda e: (-e["rating"], -e["votes"])):
        if e["m"]["id"] not in seen and len(best) < ac.get("top_episodes", 3):
            seen.add(e["m"]["id"])
            best.append(f"{len(best) + 1}. {e['ko']} {e['n']}화 ★{e['rating']:.1f}")
    EXTRA.write_text(json.dumps({"month": month, "top_episodes": best, "notes": notes}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    # 순위: 그 달 방영 회차 평점 평균. 사진 밑에 "N화 평점 X점" 줄
    shows = []
    for lst in by_media.values():
        if len(lst) >= ac.get("min_episodes", 2):
            lst = sorted({e["n"]: e for e in lst}.values(), key=lambda e: e["n"])
            avg = sum(e["rating"] for e in lst) / len(lst)
            shows.append((avg, sum(e["votes"] for e in lst), lst))
    if os.environ.get("ANIME_DEBUG"):
        for avg, votes, lst in sorted(shows, key=lambda x: -x[0]):
            print("   DBG", round(avg, 2), lst[0]["ko"], [(x["n"], x["tc"], x["rating"], x["votes"]) for x in lst])
    for rank, (avg, votes, lst) in enumerate(sorted(shows, key=lambda x: (-x[0], -x[1]))[:top], 1):
        e = lst[0]
        items.append({
            "category": cat, "source": "IMDb 작품 평점",
            "url": f"https://www.imdb.com/title/{e['sid']}/#m{month}",
            "title": f"{e['ko']} (평균 ★{avg:.1f})",  # 'N위'는 사이트에서 고른 순서대로 붙임
            "summary": "\n".join(f"{x['n']}화 평점 {x['rating']:.1f}점" for x in lst),
            "images": e["imgs"], "views": votes, "score": 2000 + avg * 10, "posted_at": posted,
        })
    items = [x for x in items if build_site.save_image(x["images"][0])]
    print(f"  [애니 평점] {month}: 작품 {len(shows)}개 중 TOP {len(items)}, 최고 회차 {best}")
    return items


if __name__ == "__main__":
    for it in collect(collector.load_config()):
        print(it["source"], it["title"], it["views"], it["summary"][:60])
