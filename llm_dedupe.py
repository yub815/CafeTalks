"""Claude API로 같은 소식(중복) 묶기.

GitHub 저장소 Secrets에 ANTHROPIC_API_KEY 가 있으면 동작하고, 없으면 아무것도 안 하고 넘어갑니다.
"""
from __future__ import annotations

import json
import os

MODEL = "claude-opus-5-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "description": "같은 소식을 다루는 항목 번호 묶음. 2개 이상인 묶음만.",
            "items": {"type": "array", "items": {"type": "integer"}},
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}

GUIDE = {
    "신상": "같은 브랜드의 같은 제품/메뉴 출시 소식이면 같은 소식이에요. 같은 브랜드라도 다른 제품이면 다른 소식이에요.",
    "게임 추천": "같은 게임을 다루면(스팀 페이지, 뉴스 기사 등 출처가 달라도) 같은 소식이에요. 영어/한국어 이름이 달라도 같은 게임이면 묶어요.",
    "신작 애니": "같은 작품의 같은 소식(개봉 흥행, 방영 확정, PV 공개 등)이면 같은 소식이에요. 같은 작품의 흥행 기록 기사 여러 개도 하나로 묶어요.",
    "유머": "같은 짤/같은 이야기를 다른 사이트에 다시 올린 글이면 같은 글이에요.",
}


def find_duplicates(category: str, items: list[dict]) -> list[list[int]]:
    """items: [{"title":..., "summary":...}] → 같은 소식 번호 묶음 리스트."""
    if len(items) < 2 or not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    import anthropic

    lines = []
    for i, it in enumerate(items):
        summary = (it.get("summary") or "").replace("\n", " ")[:150]
        lines.append(f"{i} | {it['title']} | {summary}")
    prompt = (
        f"아래는 카페 '{category}' 게시글 후보 목록이에요. 각 줄은 '번호 | 제목 | 요약'이에요.\n"
        f"같은 소식을 다루는 항목끼리 번호를 묶어 주세요. {GUIDE.get(category, '')}\n"
        "확실히 같은 소식만 묶고, 애매하면 묶지 마세요. 2개 이상인 묶음만 알려주세요.\n\n"
        + "\n".join(lines)
    )
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError:
        print("  ! Claude API 키가 올바르지 않아요. 중복 검사를 건너뜁니다.")
        return []
    except anthropic.RateLimitError:
        print("  ! Claude API 사용량 한도에 걸렸어요. 중복 검사를 건너뜁니다.")
        return []
    except anthropic.APIStatusError as e:
        print(f"  ! Claude API 오류({e.status_code}). 중복 검사를 건너뜁니다.")
        return []
    except anthropic.APIConnectionError:
        print("  ! Claude API 접속 실패. 중복 검사를 건너뜁니다.")
        return []
    if response.stop_reason in ("refusal", "max_tokens"):
        print(f"  ! Claude 응답 중단({response.stop_reason}). 중복 검사를 건너뜁니다.")
        return []
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        groups = json.loads(text).get("groups", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    valid = []
    for g in groups:
        g = sorted({i for i in g if isinstance(i, int) and 0 <= i < len(items)})
        if len(g) >= 2:
            valid.append(g)
    return valid


def dedupe(category: str, items: list[dict]) -> list[dict]:
    """같은 소식 묶음에서 점수가 가장 높은(목록에서 먼저 나온) 것 하나만 남김."""
    groups = find_duplicates(category, items)
    drop: set[int] = set()
    for g in groups:
        keep = g[0]  # items는 점수 높은 순으로 들어옴
        items[keep]["dup_count"] = len(g)
        drop.update(g[1:])
    if groups:
        print(f"  [{category}] Claude가 중복 {len(drop)}개를 묶었어요 ({len(groups)}묶음)")
    return [it for i, it in enumerate(items) if i not in drop]
