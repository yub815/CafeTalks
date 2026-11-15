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
IMDB = "https://datasets.imdbws.com/"

ANILIST_Q = """query($p:Int,$a:Int,$b:Int){Page(page:$p,perPage:50){pageInfo{hasNextPage}
airingSchedules(airingAt_greater:$a,airingAt_lesser:$b,sort:TIME){episode airingAt
media{id isAdult popularity countryOfOrigin format siteUrl synonyms bannerImage coverImage{extraLarge}
title{romaji english native}}}}}"""


def _norm(t: str) -> str:
    t = (t or "").lower()
    t = re.sub(r"(season|part|cour)\s*\d+|\d+(st|nd|rd|th)\s*(season|part|cour)|第\s*\d+\s*(期|クール)|"
               r"\b(ii|iii|iv)\b|[:：\-–!?！？.,'\"()（）\[\]「」『』・~〜]", " ", t)
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


def imdb_index(wanted: set[str], year: int):
    """애니 시리즈(제목 → tconst)와 최근 2년 회차(tconst → 년도)."""
    series: dict[str, set[str]] = {}
    eps: dict[str, int] = {}
    for c in _lines("title.basics.tsv.gz"):
        if len(c) < 9:
            continue
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
    ac = cfg.get("anime_ratings", {})
    if not ac.get("enabled", True):
        return []
    weeks = cfg.get("keep_weeks", 5)
    air = airings(weeks * 7 + 1, ac.get("min_popularity", 15000))
    print(f"  [애니 평점] AniList 방영 기록 {len(air)}개")
    names = {}
    for a in air:
        t = a["media"]["title"]
        all_t = [x for x in (t["romaji"], t.get("english"), t["native"], *(a["media"]["synonyms"] or [])) if x]
        # "JoJo's Bizarre Adventure: Steel Ball Run" 처럼 IMDb엔 앞부분 이름 시리즈로만 있는 경우도 찾게
        names[a["media"]["id"]] = {_norm(x) for x in all_t} | {
            _norm(x.split(":")[0]) for x in all_t if ":" in x and len(_norm(x.split(":")[0])) >= 8}
    wanted = set().union(*names.values()) if names else set()
    series, by_series, ep_year, ratings = imdb_index(wanted, datetime.now(collector.KST).year)
    print(f"  [애니 평점] IMDb에서 찾은 애니 {len(series)}개")
    ko = korean_titles(list({a["media"]["id"]: a["media"] for a in air}.values()))
    min_votes = ac.get("min_votes", 30)
    per_week = ac.get("per_week", 10)
    picked: dict[str, list[dict]] = {}
    for a in air:
        m, n = a["media"], a["episode"]
        aired = datetime.fromtimestamp(a["airingAt"], collector.KST)
        cands = []
        for t in names[m["id"]]:
            for sid in series.get(t, ()):
                for season, epn, tc in by_series.get(sid, []):
                    if epn == n and ep_year.get(tc) in (aired.year, aired.year - 1) and tc in ratings:
                        cands.append((season, ep_year[tc], tc))
        if not cands:
            continue
        _, _, tc = max(cands)  # 가장 최근 시즌의 그 화
        rating, votes = ratings[tc]
        if votes < min_votes:
            continue
        wk = build_site.week_of(aired.isoformat())
        title_ko = ko.get(str(m["id"])) or m["title"].get("english") or m["title"]["romaji"]
        imgs = [u for u in (m.get("bannerImage"), (m.get("coverImage") or {}).get("extraLarge")) if u]
        picked.setdefault(wk, []).append({
            "category": ac.get("category", "신작 애니"), "source": "IMDb 회차 평점",
            "url": f"https://www.imdb.com/title/{tc}/#w{wk}",
            "title": f"★{rating:.1f} {title_ko} {n}화",
            "summary": (f"IMDb 평점 {rating:.1f} ({votes}명 평가). {title_ko} 에피소드 {n}화, "
                        f"{aired.month}월 {aired.day}일 방영. 원제 {m['title']['native']} / "
                        f"{m['title'].get('english') or m['title']['romaji']}"),
            "images": imgs, "views": votes, "score": 1000 + rating * 10,  # 애니 소식보다 위, 평점 순
            "posted_at": aired.isoformat(),
        })
    items = []
    for wk, lst in picked.items():
        lst.sort(key=lambda x: -x["score"])
        items += [x for x in lst if x["images"] and build_site.save_image(x["images"][0])][:per_week]
    print(f"  [애니 평점] 평점 붙은 회차 {sum(len(v) for v in picked.values())}개 → {len(items)}개")
    return items


if __name__ == "__main__":
    for it in collect(collector.load_config()):
        print(it["posted_at"][:10], it["title"], it["views"], it["url"])
