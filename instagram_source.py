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


def collect(cfg: dict) -> list[dict]:
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
    for name in [a.lstrip("@") for a in ic["accounts"]]:
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
            ts = m.get("timestamp", "")
            if last and ts <= last:
                continue
            cap = m.get("caption") or ""
            posted = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").astimezone(collector.KST)
            if posted < old or not LAUNCH.search(cap):
                continue
            if GIVEAWAY.search(cap) and not re.search(r"출시|신제품|신메뉴", cap):
                continue  # 경품·추첨 위주 글은 뺌
            imgs = _images(m)
            if not imgs:
                continue
            body = re.sub(r"(#\S+\s*)+$", "", cap.strip()).strip()  # 끝에 몰린 해시태그 제거
            first = next((l.strip() for l in body.splitlines() if len(l.strip()) > 4), body)[:70]
            items.append({
                "category": ic.get("category", "신상"), "source": f"인스타 · @{name}",
                "url": m.get("permalink", f"https://www.instagram.com/{name}/"),
                "title": first, "summary": body[:800], "images": imgs[:4],
                "likes": m.get("like_count", 0), "comments": m.get("comments_count", 0),
                "score": 15 + (m.get("like_count") or 0) / 100, "posted_at": posted.isoformat(),
            })
        if media:
            seen[name] = max(seen.get(name, ""), *(m.get("timestamp", "") for m in media))
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  [인스타] 계정 {ok}개 확인 → 신상 후보 {len(items)}개")
    return items


if __name__ == "__main__":
    for it in collect(collector.load_config()):
        print(it["source"], it["title"], len(it["images"]))
