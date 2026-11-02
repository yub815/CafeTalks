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


# ---------------------------------------------------------------- 카페 말투로 제목줄·코멘트 쓰기
STYLE_EXAMPLES = {
    "block": (
        "예시 1)\n제목줄: 컴포즈 커피 와플\n코멘트:\n컴포즈에서 와플도 하고 있었다요\n가을 맞아서 살 많이 찌는\n"
        "신메뉴 와플들이 추가로 나왔다요\n설마 매장에서 직접 반죽 부어 굽는다요?\n\n"
        "예시 2)\n제목줄: 뚜레쥬르 미피케이크\n코멘트:\n캐릭터 머리가 통째로 올라간\n아이스크림 케이크 2종이 나왔다요\n"
        "가운데 꽂힌 게 열어보면 포크라는데\n생긴 거 보니 실용성은 별로 없어 보인다요"
    ),
    "numbered": (
        "예시)\n제목줄: 예스노탐정\n코멘트:\n'예' '아니오'로 답할 수 있는 질문을 하면 AI가 대답해주는 추리게임이다요\n"
        "질문으로 단서 모아서 정답 제출하면 된다요\n질문 수를 줄일수록 등급이 높아지니 추천한다요"
    ),
}

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "heading": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["i", "heading", "comment"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def write_comments(category: str, style: str, items: list[dict]) -> dict[int, dict]:
    """20개씩 나눠서 요청 (요청 하나가 너무 길어지지 않게)."""
    out: dict[int, dict] = {}
    for start in range(0, len(items), 20):
        part = _write_batch(category, style, items[start:start + 20])
        out.update({start + i: w for i, w in part.items()})
    if out:
        print(f"  [{category}] Claude가 코멘트 {len(out)}개를 썼어요")
    return out


def _write_batch(category: str, style: str, items: list[dict]) -> dict[int, dict]:
    """items: [{"title","summary"}] → {번호: {"heading", "comment"}}. 키가 없거나 실패하면 {}."""
    if not items or not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    import anthropic

    lines = []
    for i, it in enumerate(items):
        summary = (it.get("summary") or "").replace("\n", " ")[:300]
        lines.append(f"{i} | {it['title']} | {summary}")
    prompt = (
        f"네이버 카페의 '{category}' 모음글에 들어갈 항목별 '제목줄'과 '코멘트'를 써 주세요.\n\n"
        "말투는 아래 예시처럼 가볍고 짧은 구어체 다요체로 써 주세요. 뉴스 기사체(~했다, ~밝혔다, 기자명, 회사 설명)는 쓰지 마세요.\n"
        "- 제목줄: 브랜드/작품/게임 이름 + 제품명 정도로 짧게 (예: '이디야 뉴바이트 디저트 3종')\n"
        "- 코멘트: 2~4줄. 줄마다 짧게 끊어서 줄바꿈(\\n). 무엇이 나왔는지 + 핵심 정보 1~2개(가격, 기간 한정, 특징 등 요약에 있는 것만) + 가벼운 감상 한마디.\n"
        "- 요약에 없는 사실은 지어내지 마세요. 정보가 부족하면 짧게 쓰면 돼요.\n"
        "- 말투는 '다요체': 줄 끝을 '~다요'로 끝내세요 (나왔다요, 있다요, 좋겠다요, 보인다요, 굽는다요? 처럼). "
        "'~요/~네요/~습니다'로 끝내지 마세요. 줄이 중간에서 다음 줄로 이어지는 경우만 예외예요.\n"
        "- 문장 끝에 마침표는 찍지 마세요. 이모지는 쓰지 마세요.\n\n"
        f"{STYLE_EXAMPLES.get(style, STYLE_EXAMPLES['block'])}\n\n"
        "아래 목록의 모든 항목(번호 | 원래 제목 | 요약)에 대해 써 주세요.\n\n" + "\n".join(lines)
    )
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": WRITE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        print(f"  ! Claude API 오류({e.status_code}). 코멘트는 기사 요약으로 둡니다.")
        return {}
    except anthropic.APIConnectionError:
        print("  ! Claude API 접속 실패. 코멘트는 기사 요약으로 둡니다.")
        return {}
    if response.stop_reason in ("refusal", "max_tokens"):
        print(f"  ! Claude 응답 중단({response.stop_reason}). 코멘트는 기사 요약으로 둡니다.")
        return {}
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        rows = json.loads(text).get("items", [])
    except (json.JSONDecodeError, AttributeError):
        return {}
    out = {}
    for r in rows:
        i = r.get("i")
        if isinstance(i, int) and 0 <= i < len(items) and r.get("comment"):
            out[i] = {"heading": r.get("heading", "").strip(), "comment": r["comment"].strip().replace("다다요", "다요")}
    return out


PICK_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"}, "keep": {"type": "array", "items": {"type": "integer"}}},
        "required": ["i", "keep"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def _size(path) -> str:
    try:
        from PIL import Image
        w, h = Image.open(path).size
        return f"{w}x{h}"
    except Exception:
        return "크기 모름"


def _b64_small(path) -> str | None:
    import base64, io
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def pick_product_images(category: str, items: list[dict]) -> dict[int, list[int]]:
    """items: [{"title", "paths": [로컬 jpg 경로]}] → {번호: [제품이 보이는 사진 번호, 좋은 순]}.
    4개씩 나눠서 사진을 직접 보고 고름. 키가 없거나 실패한 항목은 결과에 없음."""
    out: dict[int, list[int]] = {}
    for start in range(0, len(items), 4):
        part = _pick_batch(category, items[start:start + 4])
        out.update({start + i: k for i, k in part.items()})
    if items:
        print(f"  [{category}] Claude가 사진 {len(out)}개 항목을 확인했어요")
    return out


def _pick_batch(category: str, items: list[dict]) -> dict[int, list[int]]:
    if not items or not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    import anthropic

    content = [{"type": "text", "text": (
        f"네이버 카페 '{category}' 모음글에 넣을 사진을 고르려고 해요. 항목마다 신상 제품 자체가 잘 보이는 사진만 골라 주세요.\n"
        "좋은 사진: 제품/패키지/메뉴 사진, 공식 홍보 이미지(제품이 크게 나온 것).\n"
        "빼야 할 사진: 사람(연예인, 모델, 방송 캡처)이 주인공이고 제품이 안 보이거나 작은 것, 매장 외관, 로고만 있는 것, "
        "다른 제품, 표/그래프, 기사 제목과 상관없는 것.\n"
        "사진마다 크기(가로x세로 픽셀)를 적어 뒀어요. 제품이 보이는 사진이면 작거나 조금 잘려도 빼지 말고, "
        "순서만 크고 선명하고 안 잘린 사진이 앞에 오게 해 주세요. 같은 사진의 작은 버전과 큰 버전이 있으면 큰 것만 넣으세요.\n"
        "keep에는 좋은 사진 번호를 좋은 순서로 넣고, 하나도 없으면 빈 배열로 주세요.")}]
    for i, it in enumerate(items):
        content.append({"type": "text", "text": f"\n[항목 {i}] {it['title']}"})
        for j, p in enumerate(it["paths"]):
            b = _b64_small(p)
            if b:
                content.append({"type": "text", "text": f"항목 {i} 사진 {j} ({_size(p)}):"})
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}})
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": PICK_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIStatusError as e:
        print(f"  ! Claude API 오류({e.status_code}). 사진 확인을 건너뜁니다.")
        return {}
    except anthropic.APIConnectionError:
        print("  ! Claude API 접속 실패. 사진 확인을 건너뜁니다.")
        return {}
    if response.stop_reason in ("refusal", "max_tokens"):
        return {}
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        rows = json.loads(text).get("items", [])
    except (json.JSONDecodeError, AttributeError):
        return {}
    out = {}
    for r in rows:
        i = r.get("i")
        if isinstance(i, int) and 0 <= i < len(items):
            out[i] = [k for k in r.get("keep", []) if isinstance(k, int) and 0 <= k < len(items[i]["paths"])]
    return out

