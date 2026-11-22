"""브랜드 공식 X(트위터) 계정의 새 글에서 신상 후보 가져오기 (하루 한 번, 새 글만).

비용 아끼기:
- 정해 둔 계정만 봄 (config.json → x_source.accounts). 검색은 안 씀.
- 계정 번호는 처음 한 번만 조회해서 data/x_state.json에 저장.
- 계정마다 지난번에 본 마지막 글 다음부터만 가져옴 (since_id). 새 글이 없으면 거의 0원.
- X API는 쓴 만큼 냄: 글 1개 읽기 $0.005, 계정 조회 1개 $0.010 (2026-11 기준).
GitHub 저장소 비밀값 X_BEARER_TOKEN 이 있어야 동작해요.
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
STATE = BASE / "data" / "x_state.json"
API = "https://api.x.com/2"
PRICE_POST, PRICE_USER = 0.005, 0.010

LAUNCH = re.compile(r"출시|신상|신제품|신메뉴|NEW|new|뉴|새로운|새롭게|런칭|론칭|등장|나왔|한정판|리뉴얼|컴백")
GIVEAWAY = re.compile(r"추첨|경품|당첨|댓글 이벤트|RT 이벤트|리트윗 이벤트|팔로우 이벤트|응모")


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def collect(cfg: dict) -> list[dict]:
    xc = cfg.get("x_source", {})
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not xc.get("enabled", True) or not token or not xc.get("accounts"):
        return []
    h = {"Authorization": f"Bearer {token}"}
    st = _load()
    ids, since = st.setdefault("ids", {}), st.setdefault("since", {})
    missing = st.setdefault("missing", [])
    cost = {"posts": 0, "users": 0}
    accounts = [a.lstrip("@") for a in xc["accounts"]]

    # 1) 계정 번호 (처음 한 번만)
    todo = [a for a in accounts if a.lower() not in {k.lower() for k in ids} and a not in missing]
    if todo:
        r = requests.get(f"{API}/users/by", headers=h, params={"usernames": ",".join(todo[:100])}, timeout=30)
        if r.status_code != 200:
            print(f"  ! X 계정 조회 실패 {r.status_code}: {r.text[:200]}")
            return []
        d = r.json()
        for u in d.get("data", []):
            ids[u["username"]] = u["id"]
            cost["users"] += 1
        found = {u["username"].lower() for u in d.get("data", [])}
        for a in todo:
            if a.lower() not in found:
                missing.append(a)
                print(f"  ! X 계정을 못 찾음: @{a} (config.json에서 이름 확인)")

    # 2) 계정별 새 글
    items: list[dict] = []
    max_new = xc.get("max_posts_per_account", 5)
    old = datetime.now(collector.KST) - timedelta(days=cfg.get("days", 7))
    for name, uid in ids.items():
        if name.lower() not in {a.lower() for a in accounts}:
            continue
        params = {"max_results": max(5, max_new), "exclude": "retweets,replies",
                  "tweet.fields": "created_at,public_metrics,attachments",
                  "expansions": "attachments.media_keys", "media.fields": "type,url,preview_image_url"}
        if since.get(name):
            params["since_id"] = since[name]
        r = requests.get(f"{API}/users/{uid}/tweets", headers=h, params=params, timeout=30)
        if r.status_code != 200:
            print(f"  ! @{name} 글 가져오기 실패 {r.status_code}: {r.text[:200]}")
            continue
        d = r.json()
        posts = d.get("data", [])
        cost["posts"] += len(posts)
        if d.get("meta", {}).get("newest_id"):
            since[name] = d["meta"]["newest_id"]
        media = {m["media_key"]: m for m in d.get("includes", {}).get("media", [])}
        for p in posts:
            text = p.get("text", "")
            posted = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).astimezone(collector.KST)
            if posted < old or not LAUNCH.search(text):
                continue
            if GIVEAWAY.search(text) and not re.search(r"출시|신제품|신메뉴", text):
                continue  # 경품·추첨 위주 글은 뺌
            imgs = []
            for k in (p.get("attachments") or {}).get("media_keys", []):
                m = media.get(k) or {}
                u = m.get("url") if m.get("type") == "photo" else m.get("preview_image_url")
                if u:
                    imgs.append(u + ("?name=large" if "pbs.twimg.com/media/" in u and "?" not in u else ""))
            if not imgs:
                continue  # 사진 없는 글은 신상글에 못 씀
            body = re.sub(r"https://t\.co/\S+", "", text).strip()
            first = next((l.strip() for l in body.splitlines() if len(l.strip()) > 4), body)[:70]
            pm = p.get("public_metrics", {})
            items.append({
                "category": xc.get("category", "신상"), "source": f"X · @{name}",
                "url": f"https://x.com/{name}/status/{p['id']}",
                "title": first, "summary": body[:800], "images": imgs[:4],
                "likes": pm.get("like_count", 0), "comments": pm.get("reply_count", 0),
                "score": 15 + pm.get("like_count", 0) / 50, "posted_at": posted.isoformat(),
            })

    # 3) 비용 기록 (data/x_state.json 의 usage: 월별)
    usd = cost["posts"] * PRICE_POST + cost["users"] * PRICE_USER
    month = datetime.now(collector.KST).strftime("%Y-%m")
    u = st.setdefault("usage", {}).setdefault(month, {"posts": 0, "users": 0, "usd": 0.0})
    u["posts"] += cost["posts"]
    u["users"] += cost["users"]
    u["usd"] = round(u["usd"] + usd, 4)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  [X] 새 글 {cost['posts']}개·계정 조회 {cost['users']}개 읽음 (약 ${usd:.3f}, 이번 달 ${u['usd']:.2f}) "
          f"→ 신상 후보 {len(items)}개")
    return items


if __name__ == "__main__":
    for it in collect(collector.load_config()):
        print(it["source"], it["title"], len(it["images"]))
