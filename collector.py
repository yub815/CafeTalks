"""주간 자료 수집기.

사용법:  python collector.py
- 커뮤니티 인기글(유머)과 뉴스(신상/신작 게임/신작 애니)를 모아 data/items.db 에 저장합니다.
- 매일 한 번씩 돌리면 한 주 동안 쌓인 것 중에서 고를 수 있어 가장 좋습니다(주 1회도 OK).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "data" / "items.db"
KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config() -> dict:
    return json.loads((BASE / "config.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- DB
def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            url TEXT PRIMARY KEY,
            category TEXT,          -- 유머 / 신상 / 신작 게임 / 신작 애니
            source TEXT,
            title TEXT,
            summary TEXT,
            images TEXT,            -- JSON 배열
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            score REAL DEFAULT 0,
            posted_at TEXT,         -- ISO, KST
            collected_at TEXT,
            used INTEGER DEFAULT 0  -- 글에 사용했으면 1
        )""")
    return con


def upsert(con: sqlite3.Connection, it: dict) -> None:
    now = datetime.now(KST).isoformat(timespec="seconds")
    old = con.execute("SELECT images, used FROM items WHERE url=?", (it["url"],)).fetchone()
    images = it.get("images") or []
    if old and not images:
        images = json.loads(old["images"] or "[]")
    con.execute("""
        INSERT INTO items (url, category, source, title, summary, images, views, likes, comments,
                           score, posted_at, collected_at, used)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title, summary=excluded.summary, images=excluded.images,
            views=MAX(views, excluded.views), likes=MAX(likes, excluded.likes),
            comments=MAX(comments, excluded.comments), score=MAX(score, excluded.score),
            collected_at=excluded.collected_at
    """, (it["url"], it["category"], it["source"], it["title"], it.get("summary", ""),
          json.dumps(images, ensure_ascii=False), it.get("views", 0), it.get("likes", 0),
          it.get("comments", 0), it.get("score", 0), it.get("posted_at") or now, now))


# ---------------------------------------------------------------- helpers
def get(url: str, **kw) -> requests.Response | None:
    for attempt in range(2):
        try:
            r = session.get(url, timeout=15, **kw)
            if "charset" not in r.headers.get("Content-Type", "").lower():
                # 인코딩 표기가 없으면 UTF-8로, 페이지 안에 euc-kr 표시가 있으면 cp949로
                head = r.content[:3000].lower()
                r.encoding = "cp949" if (b"euc-kr" in head or b"ks_c_5601" in head) else "utf-8"
            if r.status_code == 200:
                return r
            log(f"  ! {r.status_code} {url}")
            return None
        except requests.RequestException as e:
            log(f"  ! 접속 실패({attempt + 1}) {url}: {e.__class__.__name__}")
            time.sleep(2)
    return None


def to_int(s: str) -> int:
    s = (s or "").replace(",", "").strip()
    m = re.search(r"\d+", s)
    return int(m.group()) if m else 0


def parse_list_time(text: str, title_attr: str | None = None) -> str:
    """'13:40', '26/11/02', '2026.11.02', 'yyyy-mm-dd hh:mm:ss' 같은 표기를 ISO로."""
    now = datetime.now(KST)
    for raw in (title_attr, text):
        if not raw:
            continue
        raw = raw.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M", "%Y.%m.%d", "%y/%m/%d", "%y.%m.%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=KST).isoformat()
            except ValueError:
                pass
        m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", raw)
        if m:
            t = now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
            if t > now:
                t -= timedelta(days=1)
            return t.isoformat()
        m = re.fullmatch(r"(\d{2})[.-](\d{2})", raw)  # 'mm.dd'
        if m:
            t = now.replace(month=int(m[1]), day=int(m[2]), hour=12, minute=0, second=0, microsecond=0)
            if t > now:
                t = t.replace(year=t.year - 1)
            return t.isoformat()
    return now.isoformat()


BODY_SKIP = ("- dc official App", "dc official App", "출처 :", "출처:", "[원본 보기]", "원본 보기")


def body_text(box) -> str:
    """원글 본문 글자만 (줄 단위). 앱 서명·빈 줄은 빼고, 자르지 않음."""
    lines = []
    for line in box.get_text("\n", strip=True).split("\n"):
        line = line.strip()
        if not line or any(line.startswith(k) for k in BODY_SKIP) or line.startswith("http"):
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return "\n".join(lines)


def post_content(url: str, selector: str, referer: str | None = None, limit: int = 8) -> tuple[list[str], str]:
    """글 페이지에서 (짤 주소들, 본문 글)."""
    r = get(url, headers={"Referer": referer or url})
    if not r:
        return [], ""
    soup = BeautifulSoup(r.text, "html.parser")
    box = soup.select_one(selector)
    if not box:
        return [], ""
    return _box_images(box, url, limit), body_text(box)


def content_images(url: str, selector: str, referer: str | None = None, limit: int = 8) -> list[str]:
    return post_content(url, selector, referer, limit)[0]


def _box_images(box, url: str, limit: int) -> list[str]:
    out = []
    for img in box.select("img"):
        src = img.get("data-original") or img.get("data-src") or img.get("src") or ""
        if not src or src.startswith("data:") or "emoticon" in src or "dccon" in src or "thumb.php" in src \
                or "loading" in src or "/images/" in src.split("?")[0][-40:]:
            continue
        src = urljoin(url, src)
        if src not in out:
            out.append(src)
        if len(out) >= limit:
            break
    # 동영상(mp4/gif 변환)도 짤로 취급
    for v in box.select("video source, video"):
        src = v.get("src") or v.get("data-src")
        if src:
            out.append(urljoin(url, src))
    return out[:limit]


# ---------------------------------------------------------------- 유머: 디시 실베
def collect_dcinside(pages: int) -> list[dict]:
    items = []
    for p in range(1, pages + 1):
        r = get(f"https://gall.dcinside.com/board/lists/?id=dcbest&page={p}")
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        for tr in soup.select("tr.ub-content"):
            num = tr.select_one("td.gall_num")
            if not num or not num.get_text(strip=True).isdigit():
                continue  # 공지/설문
            a = tr.select_one("td.gall_tit a:not(.reply_numbox)")
            if not a:
                continue
            title = a.get_text(" ", strip=True)
            reply = tr.select_one("td.gall_tit .reply_num")
            date = tr.select_one("td.gall_date")
            items.append({
                "category": "유머", "source": "디시 실베",
                "url": re.sub(r"&page=\d+", "", urljoin("https://gall.dcinside.com", a.get("href", ""))),
                "title": re.sub(r"^\[[^\]]+\]\s*", "", title),
                "tag": (re.match(r"^\[([^\]]+)\]", title) or [None, ""])[1],  # [싱갤] 같은 갤러리 말머리
                "views": to_int(tr.select_one("td.gall_count").get_text()),
                "likes": to_int(tr.select_one("td.gall_recommend").get_text()),
                "comments": to_int(reply.get_text()) if reply else 0,
                "posted_at": parse_list_time(date.get_text() if date else "", date.get("title") if date else None),
            })
        time.sleep(1)
    return items


# ---------------------------------------------------------------- 루리웹 공통 리스트
def collect_ruliweb_list(url_tmpl: str, pages: int, category: str, source: str) -> list[dict]:
    items = []
    for p in range(1, pages + 1):
        r = get(url_tmpl.format(page=p))
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        for tr in soup.select("tr.table_body"):
            if "notice" in tr.get("class", []):
                continue
            a = tr.select_one("td.subject a.subject_link") or tr.select_one("td.subject a.deco") \
                or tr.select_one("td.subject a")
            if not a:
                continue
            reply = tr.select_one("td.subject .num_reply")
            for junk in a.select(".num_reply, .num"):
                junk.decompose()
            t = tr.select_one("td.time")
            items.append({
                "category": category, "source": source,
                "url": urljoin("https://bbs.ruliweb.com", a.get("href", "").split("?")[0]),
                "title": re.sub(r"^\d{1,2}\s+", "", a.get_text(" ", strip=True)),
                "views": to_int(tr.select_one("td.hit").get_text()) if tr.select_one("td.hit") else 0,
                "likes": to_int(tr.select_one("td.recomd").get_text()) if tr.select_one("td.recomd") else 0,
                "comments": to_int(reply.get_text()) if reply else 0,
                "posted_at": parse_list_time(t.get_text() if t else ""),
            })
        time.sleep(1)
    return items


# ---------------------------------------------------------------- 유머: 더쿠 HOT (베스트 에포트)
def collect_theqoo(pages: int) -> list[dict]:
    items = []
    for p in range(1, pages + 1):
        r = get(f"https://theqoo.net/hot?page={p}")
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        for tr in soup.select("table.theqoo_board_table tbody tr, table.bd_lst tbody tr"):
            if "notice" in " ".join(tr.get("class", [])):
                continue
            a = tr.select_one("td.title a:not(.replyNum)")
            if not a or not a.get("href"):
                continue
            reply = tr.select_one("td.title a.replyNum")
            views = tr.select_one("td.m_no")
            t = tr.select_one("td.time")
            items.append({
                "category": "유머", "source": "더쿠 HOT",
                "url": urljoin("https://theqoo.net", a["href"]),
                "title": a.get_text(" ", strip=True),
                "views": to_int(views.get_text()) if views else 0,
                "likes": 0,
                "comments": to_int(reply.get_text()) if reply else 0,
                "posted_at": parse_list_time(t.get_text() if t else ""),
            })
        time.sleep(1)
    return items


# ---------------------------------------------------------------- 유머: 웃긴대학 오늘의 베스트 (짧은 짤 위주)
def collect_humoruniv(pages: int) -> list[dict]:
    items = []
    for p in range(pages):
        r = get(f"https://web.humoruniv.com/board/humor/list.html?table=pds&st=day&pg={p}")
        if not r:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        for tr in soup.select("tr[id^=li_chk_pds]"):
            a = tr.select_one("td.li_sbj a[href*=read]")
            if not a:
                continue
            title_tag = tr.select_one("td.li_sbj span[id^=title_chk]")
            title = (title_tag or a).get_text(" ", strip=True)
            title = re.sub(r"\s*\[\d+\]\s*(답글추천.*)?$", "", title).strip()
            m = re.search(r"\[(\d+)\]", a.get_text(" ", strip=True))
            nums = [to_int(td.get_text()) for td in tr.select("td.li_und")]
            t = tr.select_one("td.li_date")
            num = re.search(r"number=(\d+)", a["href"])
            if not num:
                continue
            items.append({
                "category": "유머", "source": "웃긴대학",
                "url": f"https://web.humoruniv.com/board/humor/read.html?table=pds&number={num.group(1)}",
                "title": title,
                "views": nums[0] if nums else 0,
                "likes": nums[1] if len(nums) > 1 else 0,
                "comments": int(m.group(1)) if m else 0,
                "posted_at": parse_list_time(t.get_text() if t else ""),
            })
        time.sleep(1)
    return items


IMAGE_SELECTORS = {
    "웃긴대학": ("div.body_editor", "https://web.humoruniv.com/"),
    "디시 실베": ("div.write_div", "https://gall.dcinside.com/"),
    "루리웹 유머 BEST": ("div.view_content", None),
    "루리웹 게임뉴스": ("div.view_content", None),
    "더쿠 HOT": ("article div.rd_body, div.xe_content", None),
}


def humor_score(items: list[dict]) -> None:
    """사이트마다 숫자 규모가 달라서, 사이트 안에서의 순위(0~100)로 바꿔 합칩니다."""
    by_src: dict[str, list[dict]] = {}
    for it in items:
        by_src.setdefault(it["source"], []).append(it)
    for lst in by_src.values():
        raw = [it["views"] + it["likes"] * 30 + it["comments"] * 5 for it in lst]
        order = sorted(range(len(lst)), key=lambda i: raw[i])
        n = max(len(lst) - 1, 1)
        for rank, i in enumerate(order):
            lst[i]["score"] = round(rank / n * 100, 1)


# ---------------------------------------------------------------- 뉴스 (Bing 뉴스 RSS)
def bing_news(query: str) -> list[dict]:
    url = f"https://www.bing.com/news/search?q={quote(query)}&format=rss&setlang=ko&cc=KR&qft=sortbydate%3d%221%22"
    r = get(url)
    if not r:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    ns = {"News": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    out = []
    for item in root.iter("item"):
        link = item.findtext("link") or ""
        real = parse_qs(urlparse(link).query).get("url", [link])[0]
        img = None
        for child in item:
            if child.tag.endswith("Image"):
                img = child.text
        try:
            posted = parsedate_to_datetime(item.findtext("pubDate")).astimezone(KST).isoformat()
        except Exception:
            posted = datetime.now(KST).isoformat()
        src = ""
        for child in item:
            if child.tag.endswith("Source"):
                src = child.text or ""
        out.append({
            "url": real, "title": (item.findtext("title") or "").strip(),
            "summary": (item.findtext("description") or "").strip(),
            "image": (img + "&w=800") if img else None, "posted_at": posted, "press": src,
        })
    return out


def og_image(url: str) -> str | None:
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    tag = soup.select_one('meta[property="og:image"], meta[name="og:image"], meta[name="twitter:image"]')
    return urljoin(url, tag["content"]) if tag and tag.get("content") else None


def untumb(u: str) -> str | None:
    v = re.sub(r"(_\d+)?_(thum|thumb|s)(?=\.(jpe?g|png|webp)$)", "", u, flags=re.I)
    # 국내 뉴스 사이트 공통 형식: /news/thumbnail/202611/123_456_789_v150.jpg → /news/photo/202611/123_456_789.jpg
    v = re.sub(r"/news/thumbnail/(\d+/[\w]+?)_v\d+(\.\w+)$", r"/news/photo/\1\2", v)
    v = re.sub(r"-\d{2,4}x\d{2,4}(?=\.(jpe?g|png|webp)$)", "", v, flags=re.I)  # 워드프레스 -300x188
    v = re.sub(r"/(thumb|thumbnail|thumbs)/", "/", v, flags=re.I)
    return v if v != u else None


def article_images(url: str, limit: int = 6) -> list[str]:
    """기사 본문 사진들 (대표 이미지 + 본문 img). 로고·아이콘은 뺌."""
    r = get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    found = []
    tag = soup.select_one('meta[property="og:image"], meta[name="og:image"], meta[name="twitter:image"]')
    og = urljoin(url, tag["content"]) if tag and tag.get("content") else None
    body = soup.select_one("article, [itemprop=articleBody], #articleBody, #article-view-content-div, "
                           ".article_body, .article-body, #article_body, .news_body, #newsct_article, .view_con") or soup
    for img in body.select("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src") or ""
        if not src or src.startswith("data:") or src.endswith((".svg", ".gif")):
            continue
        try:
            if int(img.get("width") or 999) < 200:
                continue
        except ValueError:
            pass
        found.append(urljoin(url, src))
    # 대표 이미지(og)는 잘린 썸네일인 경우가 많아서 본문 사진 뒤에 둠
    if og:
        found.append(og)
    # 썸네일 주소면 원본 주소도 시도 (예: ..._750_thum.jpg → ....jpg)
    found = [v for u in found for v in (untumb(u), u) if v]
    out = []
    for u in found:
        if u not in out and not is_logo(u):
            out.append(u)
    return out[:limit]


LOGO_HINTS = ("logo", "default", "og_image", "ogimage", "og-image", "noimage", "no_image", "sns_", "share",
              "symbol", "ci.", "/ci/", "favicon", "banner")
NEWS_EXCLUDE_DEFAULT = ["칼럼", "사설", "기고", "오피니언", "기자수첩", "인터뷰", "취재", "분석", "전망", "질문",
                        "주가", "증권", "실적", "매출", "영업이익", "투자", "논란", "소송", "부고",
                        "업계", "트렌드", "동향", "外", "경쟁"]
ACTION_WORDS = ["출시", "선보", "공개", "개봉", "방영", "발매", "론칭", "런칭", "사전예약", "오픈", "판매", "신메뉴",
                "돌파", "확정", "업데이트", "PV", "티저", "예고"]


def is_logo(url: str | None) -> bool:
    return not url or any(h in url.lower() for h in LOGO_HINTS)


GENERIC = {"출시", "신제품", "신메뉴", "공개", "시즌", "한정", "애니메이션", "애니", "돌파", "게임", "스팀",
           "겨울", "선보여", "선봬", "브랜드", "누적", "관객", "인기", "화제", "흥행", "메뉴", "제품", "종", "2종",
           "3종", "4종", "5종", "기념", "판매", "업계", "이벤트"}
PARTICLE = re.compile(r"(이|가|은|는|을|를|의|에|도|와|과|로|으로|에서|까지|부터)$")


def tokens(t: str) -> set[str]:
    t = re.sub(r"\[[^\]]*\]|【[^】]*】", " ", t)
    out = set()
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", t):
        w = PARTICLE.sub("", w) if len(w) > 2 else w
        if len(w) >= 2 and w not in GENERIC:
            out.add(w.lower())
    return out


def quoted(t: str) -> set[str]:
    return {q.strip() for q in re.findall(r"[‘'“\"]([^’'”\"]{2,30})[’'”\"]", t)}


def brands(t: str) -> set[str]:
    """'투썸플레이스, ...' 처럼 쉼표 앞에 오는 회사/브랜드 이름."""
    return {w for w in re.findall(r"([가-힣A-Za-z0-9&]{2,})\s*[,，]", t)}


def same_story(a: dict, b: dict) -> bool:
    if a.get("image") and a.get("image") == b.get("image"):
        return True
    if brands(a["title"]) & brands(b["title"]):
        try:
            gap = abs(datetime.fromisoformat(a["posted_at"]) - datetime.fromisoformat(b["posted_at"]))
        except (KeyError, ValueError):
            gap = timedelta(0)
        if gap <= timedelta(days=2):
            return True  # 같은 브랜드가 이틀 안에 낸 기사 = 같은 소식으로 봄
    qa, qb = quoted(a["title"]), quoted(b["title"])
    if qa & qb:
        return True
    ta, tb = tokens(a["title"]), tokens(b["title"])
    if not ta or not tb:
        return False
    common = ta & tb
    if len([w for w in common if len(w) >= 3]) >= 2:
        return True  # '극장판'+'치이카와', 'cgv'+'트러플' 처럼 고유한 단어 2개 이상 겹치면 같은 소식
    return len(common) / min(len(ta), len(tb)) >= 0.5


def collect_news(cat: str, conf: dict, limit: int, since: datetime) -> list[dict]:
    clusters: list[dict] = []
    excl = NEWS_EXCLUDE_DEFAULT + conf.get("exclude_words", [])
    for q in conf.get("news_queries", []):
        log(f"  - 뉴스 검색: {q}")
        for n in bing_news(q):
            title = n["title"]
            text = title + " " + n["summary"]
            if datetime.fromisoformat(n["posted_at"]) < since:
                continue
            inc = conf.get("include_words") or []
            if inc and not any(w.lower() in text.lower() for w in inc):
                continue
            if any(w.lower() in title.lower() for w in excl):
                continue  # 칼럼·분석·주가 같은 글은 제목에 걸리면 제외
            if not any(w.lower() in title.lower() for w in conf.get("action_words", ACTION_WORDS)):
                continue  # '출시/개봉/공개' 같은 구체적인 소식만
            for c in clusters:
                if same_story(c, n):
                    c["dup"] += 1  # 여러 언론이 다룬 소식일수록 점수 업
                    if len(n["summary"]) > len(c["summary"]):
                        c["summary"] = n["summary"]
                    break
            else:
                n["dup"] = 1
                clusters.append(n)
        time.sleep(1)
    news = sorted(clusters, key=lambda n: (n["dup"], n["posted_at"]), reverse=True)
    items: list[dict] = []
    used_images: set[str] = set()
    for n in news:
        if len(items) >= limit:
            break
        og = og_image(n["url"])
        img = og if not is_logo(og) else None
        img = img or (n.get("image") if not is_logo(n.get("image")) else None)
        if not img or img in used_images:
            continue  # 사진 한 장 + 글 몇 줄 형식이라, 쓸 만한 사진이 없으면 뺌
        used_images.add(img)
        items.append({
            "category": cat, "source": f"뉴스 · {n['press']}".strip(" ·"),
            "url": n["url"], "title": n["title"], "summary": n["summary"],
            "images": [img], "score": n["dup"] * 10,
            "posted_at": n["posted_at"],
        })
    return items


def dedupe_humor(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for it in sorted(items, key=lambda h: h.get("score", 0), reverse=True):
        ta = tokens(it["title"])
        if any(ta and len(ta & tokens(o["title"])) / max(min(len(ta), len(tokens(o["title"]))), 1) >= 0.6
               for o in out):
            continue
        out.append(it)
    return out


def is_controversial(title: str, words: list[str]) -> bool:
    t = title.lower().replace(" ", "")
    return any(w.lower().replace(" ", "") in t for w in words)


# ---------------------------------------------------------------- 게임 추천 (스팀)
def steam_details(appid: str) -> dict:
    r = get(f"https://store.steampowered.com/api/appdetails?appids={appid}&l=koreana&cc=kr")
    try:
        d = r.json()[appid]["data"] if r else {}
    except Exception:
        d = {}
    shots = [x.get("path_full") for x in d.get("screenshots", [])[:3] if x.get("path_full")]
    return {"summary": BeautifulSoup(d.get("short_description", ""), "html.parser").get_text(" ", strip=True),
            "images": shots or ([d["header_image"]] if d.get("header_image") else [])}


def collect_steam(cat: str, conf: dict, since_days: int) -> list[dict]:
    items = []
    for name, extra in conf.items():
        if not isinstance(extra, str):
            continue
        url = ("https://store.steampowered.com/search/results/?query&start=0&count=50&infinite=1"
               f"&cc=kr&l=koreana&{extra}")
        log(f"  - 스팀: {name}")
        r = get(url)
        if not r:
            continue
        try:
            html_ = r.json().get("results_html", "")
        except ValueError:
            continue
        soup = BeautifulSoup(html_, "html.parser")
        for rank, a in enumerate(soup.select("a.search_result_row")):
            appid = a.get("data-ds-appid", "").split(",")[0]
            title = a.select_one(".title")
            rel = a.select_one(".search_released")
            rev = a.select_one(".search_review_summary")
            m = re.search(r"(\d{4})년 (\d{1,2})월 (\d{1,2})일", rel.get_text() if rel else "")
            if not appid or not title or not m:
                continue
            released = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=KST)
            if released < datetime.now(KST) - timedelta(days=since_days):
                continue  # 너무 예전 게임은 제외 (요즘 뜨는 것만)
            tip = rev.get("data-tooltip-html", "") if rev else ""
            reviews = to_int(re.search(r"평가 ([\d,]+)개", tip).group(1)) if re.search(r"평가 ([\d,]+)개", tip) else 0
            items.append({
                "category": cat, "source": f"스팀 · {name}",
                "url": f"https://store.steampowered.com/app/{appid}/", "appid": appid,
                "title": title.get_text(strip=True), "likes": reviews,
                "score": max(100 - rank * 2, 1) + min(reviews / 100, 50),
                "posted_at": datetime.now(KST).isoformat(),
            })
        time.sleep(1)
    out, seen = [], set()
    for it in sorted(items, key=lambda x: x["score"], reverse=True):
        if it["appid"] in seen:
            continue
        seen.add(it["appid"])
        det = steam_details(it["appid"])
        it.update(det)
        if it["images"]:
            out.append(it)
        time.sleep(0.4)
        if len(out) >= 25:
            break
    return out


# ---------------------------------------------------------------- main
def main(humor_only: bool = False) -> None:
    cfg = load_config()
    days = cfg.get("days", 7)
    since = datetime.now(KST) - timedelta(days=days)
    con = db()
    total = 0
    # 주차별로 지난 주 자료도 보여주니, 보관 기간(keep_weeks)보다 오래된 것만 지움
    old = (datetime.now(KST) - timedelta(weeks=cfg.get("keep_weeks", 5) + 1)).isoformat()
    con.execute("DELETE FROM items WHERE posted_at < ?", (old,))
    con.execute("DELETE FROM items WHERE source LIKE 'itch%'")  # itch.io 게임은 이제 안 씀
    con.commit()

    # 1) 유머
    hs = cfg.get("humor_sources", {})
    humor: list[dict] = []
    jobs = [
        ("dcinside_best", "디시 실베", lambda c: collect_dcinside(c.get("pages", 3))),
        ("ruliweb_humor_best", "루리웹 유머 BEST",
         lambda c: collect_ruliweb_list("https://bbs.ruliweb.com/best/humor_only?page={page}",
                                        c.get("pages", 2), "유머", "루리웹 유머 BEST")),
        ("theqoo_hot", "더쿠 HOT", lambda c: collect_theqoo(c.get("pages", 2))),
        ("humoruniv_best", "웃긴대학", lambda c: collect_humoruniv(c.get("pages", 2))),
    ]
    for key, name, fn in jobs:
        c = hs.get(key, {})
        if not c.get("enabled", False):
            continue
        log(f"[유머] {name} 수집 중...")
        try:
            got = fn(c)
        except Exception as e:  # 사이트 구조가 바뀌어도 나머지는 계속
            log(f"  ! {name} 실패: {e}")
            got = []
        log(f"  → {len(got)}개")
        humor += got
    humor = [h for h in humor if datetime.fromisoformat(h["posted_at"]) >= since]
    bad = cfg.get("humor_exclude_words", [])
    before = len(humor)
    humor = [h for h in humor if not is_controversial(h.get("tag", "") + " " + h["title"], bad)]
    log(f"[유머] 논란 소지 단어로 {before - len(humor)}개 제외")
    humor_score(humor)
    humor = dedupe_humor(humor)
    top = humor[: max(cfg.get("humor_candidates", 30) * 2, 20)]
    log(f"[유머] 상위 {len(top)}개 글에서 짤 가져오는 중...")
    for h in top:
        sel, ref = IMAGE_SELECTORS.get(h["source"], ("body", None))
        h["images"], h["summary"] = post_content(h["url"], sel, ref)
        time.sleep(0.5)
    # 여러 글에 똑같이 붙는 사진(사이트 배너·서명 이미지)은 짤이 아니라서 뺌
    seen: dict[str, int] = {}
    for h in top:
        for u in set(h.get("images", [])):
            seen[u] = seen.get(u, 0) + 1
    for h in top:
        h["images"] = [u for u in h.get("images", []) if seen.get(u, 0) < 2]
        if not h["images"]:
            continue  # 짤 없는 글은 제외
        upsert(con, h)
        total += 1
    con.commit()
    # 방송(치지직)에서 유행하는 게임: 3시간마다 방송 목록을 찍어 두고 순위 매김
    try:
        import stream_games
        stream_games.snapshot(cfg)
        got = stream_games.collect(cfg)
        if got:  # 매번 전부 다시 계산하니 예전 결과는 지우고 새로 넣음
            con.execute("DELETE FROM items WHERE source='치지직 방송'")
        for it in got:
            upsert(con, it)
        con.commit()
    except Exception as e:
        log(f"  ! 방송 게임 수집 실패: {e}")
    if humor_only:
        log(f"\n유머만 갱신 완료! {total}개 저장됨")
        con.close()
        return

    # 2) 루리웹 게임 뉴스 (신작 게임 보강)
    for cat, conf in cfg.get("categories", {}).items():
        extra: list[dict] = []
        if conf.get("ruliweb_news"):
            log(f"[{cat}] 루리웹 게임뉴스 수집 중...")
            try:
                extra = collect_ruliweb_list("https://bbs.ruliweb.com/news/board/1001?page={page}", 2,
                                             cat, "루리웹 게임뉴스")
            except Exception as e:
                log(f"  ! 실패: {e}")
            extra = [e for e in extra if datetime.fromisoformat(e["posted_at"]) >= since]
            humor_score(extra)
            for e in extra[:20]:
                e["images"] = content_images(e["url"], "div.view_content", None, limit=3)
                upsert(con, e)
                total += 1
                time.sleep(0.5)
        if conf.get("steam"):
            log(f"[{cat}] 스팀 인기 게임 수집 중...")
            for it in collect_steam(cat, conf["steam"], conf.get("steam_release_days", 60)):
                upsert(con, it)
                total += 1
        log(f"[{cat}] 뉴스 수집 중...")
        for it in collect_news(cat, conf, cfg.get("news_per_category", 40), since):
            upsert(con, it)
            total += 1
        con.commit()

    log(f"\n완료! {total}개 저장됨 → {DB_PATH}")
    log("이제 run.bat(또는 python app.py)으로 고르기 화면을 여세요.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
