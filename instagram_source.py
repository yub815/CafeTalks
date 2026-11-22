"""브랜드 공식 인스타그램 계정의 새 글에서 신상 후보 가져오기 (하루 한 번).

Instagram Graph API 의 Business Discovery 로 공개 비즈니스 계정의 최근 글을 읽어요 (무료).
- 정해 둔 계정만 봄 (config.json → instagram_source.accounts).
- 계정마다 지난번에 본 글 다음 것만 신상 후보로 씀 (data/ig_state.json).
GitHub 저장소 비밀값 IG_ACCESS_TOKEN (만료 없는 시스템 사용자 토큰)이 있어야 동작해요.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

import collector

BASE = Path(__file__).parent
STATE = BASE / "data" / "ig_state.json"
API = "https://graph.facebook.com/v21.0"

LAUNCH = re.compile(r"출시|신상|신제품|신메뉴|NEW|New|new|뉴|새로운|새롭게|런칭|론칭|등장|나왔|한정판|리뉴얼|컴백|단독")
GIVEAWAY = re.compile(r"추첨|경품|당첨|댓글 이벤트|팔로우 이벤트|응모|리그램 이벤트")
FIELDS = ("username,media.limit({n}){{id,caption,media_type,media_url,thumbnail_url,permalink,"
          "timestamp,like_count,comments_count,children{{media_type,media_url,thumbnail_url}}}}")


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _get(path: str, token: str, **params) -> dict:
    r = requests.get(f"{API}/{path}", params={**params, "access_token": token}, timeout=30)
    d = r.json() if r.headers.get("content-type", "").startswith(("application/json", "text/javascript")) else {}
    if r.status_code != 200:
        msg = (d.get("error") or {}).get("message", r.text[:200])
        raise RuntimeError(f"{r.status_code} {msg}")
    return d


def my_ig_id(token: str, st: dict) -> str | None:
    """토큰에 연결된 페이지의 인스타 비즈니스 계정 번호 (처음 한 번만 찾고 저장)."""
    if st.get("ig_id"):
        return st["ig_id"]
    d = _get("me/accounts", token, fields="name,instagram_business_account")
    for p in d.get("data", []):
        ig = (p.get("instagram_business_account") or {}).get("id")
        if ig:
            st["ig_id"] = ig
            print(f"  [인스타] 내 인스타 계정 번호 찾음 (페이지 {p.get('name')})")
            return ig
    print("  ! 인스타: 토큰에 연결된 페이지에서 인스타 계정을 못 찾음 (시스템 사용자에 페이지·인스타 자산 할당 확인)")
    return None


def _images(m: dict) -> list[str]:
    kids = (m.get("children") or {}).get("data") or [m]
    out = []
    for k in kids:
        u = k.get("media_url") if k.get("media_type") == "IMAGE" else k.get("thumbnail_url")
        if u:
            out.append(u)
    return out


def due_evening() -> bool:
    """오늘 저녁(17시 이후)에 아직 안 읽었으면 True."""
    last = _load().get("last_collect", "")
    now = datetime.now(collector.KST)
    return now.hour >= 17 and last < now.replace(hour=17, minute=0, second=0, microsecond=0).isoformat()


def collect(cfg: dict, force_discover: bool = False) -> list[dict]:
    ic = cfg.get("instagram_source", {})
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not ic.get("enabled", True) or not token or not ic.get("accounts"):
        return []
    st = _load()
    try:
        me = my_ig_id(token, st)
    except Exception as e:
        print(f"  ! 인스타 토큰 확인 실패: {e}")
        return []
    if not me:
        return []
    seen = st.setdefault("seen", {})
    missing = st.setdefault("missing", [])
    old = datetime.now(collector.KST) - timedelta(days=cfg.get("days", 7))
    n = ic.get("posts_per_account", 8)
    items: list[dict] = []
    ok = 0
    found = st.setdefault("found", {})
    mentions = st.setdefault("mentions", {})
    names = list(dict.fromkeys([a.lstrip("@") for a in ic["accounts"]] + list(found)))
    for name in names[: ic.get("max_accounts", 40)]:
        if name in missing:
            continue
        try:
            d = _get(me, token, fields=f"business_discovery.username({name}){{{FIELDS.format(n=n)}}}")
        except Exception as e:
            if "Invalid user id" in str(e) or "cannot be found" in str(e) or "(#110)" in str(e):
                missing.append(name)
                print(f"  ! 인스타 계정을 못 찾음 (비즈니스 계정이 아니거나 이름 틀림): @{name}")
            else:
                print(f"  ! @{name} 인스타 글 가져오기 실패: {e}")
            continue
        ok += 1
        media = ((d.get("business_discovery") or {}).get("media") or {}).get("data", [])
        last = seen.get(name, "")
        for m in media:
            if not last or m.get("timestamp", "") > last:  # 새 글에서 언급된 다른 계정 (새 계정 찾기용)
                for u in set(MENTION.findall(m.get("caption") or "")):
                    u = u.rstrip(".").lower()
                    if u != name.lower():
                        mentions[u] = mentions.get(u, 0) + 1
            ts = m.get("timestamp", "")
            if last and ts <= last:
                continue
            cap = re.sub(r"[\u2800\u200b\u3164]+", " ", m.get("caption") or "")
            posted = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(collector.KST)
            if posted < old or not LAUNCH.search(cap):
                continue
            head = cap.strip().split("\n", 1)[0]
            if (GIVEAWAY.search(cap) or re.search(r"EVENT|이벤트", head, re.I)) \
                    and not re.search(r"출시|신제품|신메뉴", cap):
                continue  # 경품·추첨·이벤트 위주 글은 뺌
            imgs = _images(m)
            if not imgs:
                continue
            body = re.sub(r"(#\S+\s*)+$", "", cap.strip()).strip()  # 끝에 몰린 해시태그 제거
            first = next((l.strip() for l in body.splitlines()
                          if len(re.sub(r"#\S+|[^\w가-힣]", "", l)) > 4), body)[:70]
            items.append({
                "category": ic.get("category", "신상"), "source": f"인스타 · @{name}",
                "url": m.get("permalink", f"https://www.instagram.com/{name}/"),
                "title": first, "summary": body[:800], "images": imgs[:4],
                "likes": m.get("like_count", 0), "comments": m.get("comments_count", 0),
                "score": 15 + (m.get("like_count") or 0) / 100, "posted_at": posted.isoformat(),
            })
        if media:
            seen[name] = max(seen.get(name, ""), *(m.get("timestamp", "") for m in media))
    st["last_collect"] = datetime.now(collector.KST).isoformat(timespec="minutes")
    print(f"  [인스타] 계정 {ok}개 확인 → 신상 후보 {len(items)}개")
    # 일주일에 한 번: 따라갈 새 브랜드 계정 찾기
    last_disc = st.get("last_discover", "")
    if (force_discover and (not last_disc or datetime.now(collector.KST) - datetime.fromisoformat(last_disc) >= timedelta(hours=6))) \
            or not last_disc or datetime.now(collector.KST) - datetime.fromisoformat(last_disc) >= timedelta(days=7):
        try:
            discover(cfg, token, me, st, names)
            st["last_discover"] = datetime.now(collector.KST).isoformat(timespec="minutes")
        except Exception as e:
            print(f"  ! 인스타 새 계정 찾기 실패: {e}")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    return items


MENTION = re.compile(r"@([A-Za-z0-9._]{3,30})")
DISC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"],
               "properties": {"items": {"type": "array", "items": {"type": "string"}}}}


def _ask(what: str, prompt: str) -> list[str]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    import anthropic
    import llm_dedupe
    resp = llm_dedupe.create(anthropic.Anthropic(), what, model=llm_dedupe.MODEL, max_tokens=4000,
                             output_config={"effort": "low", "format": {"type": "json_schema", "schema": DISC_SCHEMA}},
                             messages=[{"role": "user", "content": prompt}])
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return [x.strip().lstrip("@").lower() for x in json.loads(text).get("items", []) if x.strip()]


def discover(cfg: dict, token: str, me: str, st: dict, following: list[str]) -> None:
    """새로 따라갈 만한 브랜드 공식 계정 찾기 (주 1회).
    후보 = 따라가는 계정 글에서 언급된 계정 + Claude가 떠올린 한국 식품·음료·편의점·프랜차이즈 공식 계정 이름.
    실제로 있는지 인스타에서 확인 → 팔로워 1만 이상·최근 30일 안에 글 있음 → Claude가 '공식 브랜드 계정'인지 판정."""
    ic = cfg.get("instagram_source", {})
    found, rejected = st.setdefault("found", {}), st.setdefault("rejected", [])
    missing = st.setdefault("missing", [])
    known = {x.lower() for x in following + rejected + missing}
    mentions = st.get("mentions", {})
    cands = [u for u, _ in sorted(mentions.items(), key=lambda x: -x[1]) if u not in known][:30]
    try:  # 이번 주 신상 뉴스 제목 (어떤 브랜드가 신상을 냈는지 힌트)
        con = collector.db()
        news = [r[0] for r in con.execute(
            "SELECT title FROM items WHERE category='신상' AND source NOT LIKE '인스타%' ORDER BY posted_at DESC LIMIT 80")]
    except Exception:
        news = []
    brands = _ask("인스타 브랜드 후보", (
        "아래는 이번 주 한국 신상 뉴스 제목이에요. 여기 나온 브랜드와, 그 밖에 먹거리 신제품을 자주 내는 한국 브랜드"
        "(과자·라면·음료·아이스크림·편의점·카페·패스트푸드·치킨·베이커리) 중 아래 '이미 따라감'에 없는 곳 15개를 골라, "
        "각 브랜드의 영문 이름을 소문자로 적어 주세요 (띄어쓰기 없이, 예: orion, crown, binggrae, megacoffee). "
        "영문 표기가 두 가지면 둘 다 적어도 돼요.\n\n이미 따라감: " + ", ".join(following)
        + "\n\n뉴스 제목:\n" + "\n".join(news)))
    guess = []
    for b0 in brands:
        b0 = re.sub(r"[^a-z0-9]", "", b0)
        if len(b0) < 2:
            continue
        guess += [b0, f"{b0}_official", f"{b0}_kr", f"{b0}_korea", f"{b0}korea", f"{b0}.official", f"{b0}official", f"{b0}_kor"]
    cands += [u for u in guess if u not in known and u not in cands]
    ok, old = [], datetime.now(collector.KST) - timedelta(days=30)
    for u in cands[: ic.get("discover_max_checks", 150)]:
        try:
            d = _get(me, token, fields=f"business_discovery.username({u}){{username,name,biography,"
                                        "followers_count,media.limit(3){timestamp,caption}}")
        except Exception as e:
            if "Invalid user id" in str(e) or "cannot be found" in str(e) or "(#110)" in str(e):
                missing.append(u)
            continue
        b = d.get("business_discovery") or {}
        media = (b.get("media") or {}).get("data", [])
        recent = media and datetime.strptime(media[0]["timestamp"], "%Y-%m-%dT%H:%M:%S%z") >= old
        if (b.get("followers_count") or 0) < ic.get("discover_min_followers", 10000) or not recent:
            rejected.append(u)
            continue
        cap = " / ".join((m.get("caption") or "")[:80].replace("\n", " ") for m in media)
        ok.append((u, f"@{u} | {b.get('name', '')} | 팔로워 {b.get('followers_count')} | "
                      f"소개: {(b.get('biography') or '')[:100]} | 최근 글: {cap}"))
    if not ok:
        print(f"  [인스타] 새 계정 찾기: 후보 {len(cands)}개 중 조건 맞는 계정 없음")
        return
    yes = set(_ask("인스타 계정 판정", (
        "아래 인스타그램 계정 중 '한국 먹거리 브랜드의 공식 계정'(과자·라면·음료·아이스크림·편의점·카페·"
        "패스트푸드·치킨 등, 신제품 소식을 올리는 곳)만 아이디로 골라 주세요. 개인·리뷰어·매장 지점·"
        "연예인·해외 계정·화장품/패션 등 먹거리가 아닌 브랜드는 빼 주세요.\n\n"
        + "\n".join(line for _, line in ok))))
    today = datetime.now(collector.KST).strftime("%Y-%m-%d")
    added = []
    for u, _ in ok:
        if u in yes:
            found[u] = today
            added.append(u)
        else:
            rejected.append(u)
    print(f"  [인스타] 새 계정 찾기: 후보 {len(cands)}개 확인 → 새로 따라감: "
          + (", ".join("@" + u for u in added) or "없음"))


if __name__ == "__main__":
    for it in collect(collector.load_config()):
        print(it["source"], it["title"], len(it["images"]))


def prune(con) -> None:
    """인스타에서 가져온 글 중 '새 제품 출시'가 아닌 글(이벤트·캠페인·매장 오픈·굿즈 등)을 Claude가 걸러서 지움.
    판정 결과는 data/ig_state.json 의 judged 에 저장해서 같은 글을 다시 묻지 않음."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return
    st = _load()
    judged = st.setdefault("judged", {})
    rows = [r for r in con.execute("SELECT url, source, title, summary FROM items WHERE source LIKE '인스타%'")
            if r[0] not in judged]
    for i in range(0, len(rows), 40):
        chunk = rows[i:i + 40]
        lines = [f"{n} | {r[1]} | {(r[3] or r[2] or '')[:300].replace(chr(10), ' ')}" for n, r in enumerate(chunk)]
        keep = _ask("인스타 신상 판정", (
            "아래는 먹거리 브랜드 인스타그램 글이에요 (번호 | 계정 | 내용). 각 글이 '새로 나온 먹거리 제품·메뉴를 "
            "소개하는 글'인지 판정해서, 맞는 글의 번호만 적어 주세요.\n"
            "맞음: 신제품·신메뉴·한정판·시즌 메뉴 출시, 리뉴얼 출시, 콜라보 제품 출시.\n"
            "아님: 이벤트·경품·인증 챌린지·할인/프로모션·매장 오픈·팝업스토어·굿즈·광고 캠페인·기념일 인사·"
            "기존 메뉴 홍보·화장품 등 먹거리가 아닌 제품. 애매하면 빼 주세요.\n\n" + "\n".join(lines)))
        ok = {int(x) for x in keep if x.isdigit()}
        for n, r in enumerate(chunk):
            judged[r[0]] = n in ok
            if n not in ok:
                con.execute("DELETE FROM items WHERE url = ?", (r[0],))
                print(f"  - 인스타 신상 아님, 뺌: {r[1]} {(r[2] or '')[:40]}")
    con.commit()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
