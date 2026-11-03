"""사이트에서 '게임 요청'을 하면 (GitHub 이슈로 들어옴) 스팀에서 찾아서 게임 추천 목록 맨 위에 넣기.

사용: python request_game.py "게임 이름"   → 결과 한 줄을 출력 (이슈 댓글로 씀)
"""
from __future__ import annotations

import sys
from datetime import datetime
from urllib.parse import quote

import collector


def find_on_steam(name: str) -> dict | None:
    r = collector.get(f"https://store.steampowered.com/api/storesearch/?term={quote(name)}&l=koreana&cc=KR")
    try:
        found = r.json().get("items", []) if r else []
    except ValueError:
        found = []
    if not found:
        return None
    low = name.lower().replace(" ", "")
    # 이름이 똑같은 게임이 있으면 그걸, 아니면 첫 번째 검색 결과
    best = next((g for g in found if g.get("name", "").lower().replace(" ", "") == low), found[0])
    return {"appid": str(best["id"]), "title": best["name"]}


def main(name: str) -> int:
    name = name.strip()
    if not name:
        print("게임 이름이 비어 있어요.")
        return 1
    game = find_on_steam(name)
    if not game:
        print(f"스팀에서 '{name}'을(를) 찾지 못했어요. 영어 이름이나 정확한 이름으로 다시 요청해 주세요.")
        return 1
    det = collector.steam_details(game["appid"])
    cfg = collector.load_config()
    cat = next((k for k, v in cfg.get("categories", {}).items() if v.get("steam")), "게임 추천")
    con = collector.db()
    collector.upsert(con, {
        "category": cat, "source": "스팀 · 직접 요청",
        "url": f"https://store.steampowered.com/app/{game['appid']}/",
        "title": game["title"], "summary": det["summary"], "images": det["images"],
        "score": 1000,  # 직접 요청한 게임은 목록 맨 위로
        "posted_at": datetime.now(collector.KST).isoformat(),
    })
    # 이미 있던 게임이면 이번 주로 다시 올림
    con.execute("UPDATE items SET posted_at=?, score=1000, source='스팀 · 직접 요청' WHERE url=?",
                (datetime.now(collector.KST).isoformat(), f"https://store.steampowered.com/app/{game['appid']}/"))
    con.commit()
    con.close()
    print(f"'{game['title']}'을(를) 이번 주 게임 추천 맨 위에 넣었어요. "
          f"https://store.steampowered.com/app/{game['appid']}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:])))
