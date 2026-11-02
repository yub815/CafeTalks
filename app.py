"""고르기 화면 + 완성 글 만들기.

사용법:  python app.py   →  브라우저에서 http://127.0.0.1:5000 이 열립니다.
"""
from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from flask import Flask, Response, abort, redirect, render_template_string, request, url_for

from collector import DB_PATH, KST, UA, db, is_controversial, load_config

BASE = Path(__file__).resolve().parent
OUT = BASE / "output"
app = Flask(__name__)
http = requests.Session()
http.headers.update({"User-Agent": UA})


# ---------------------------------------------------------------- 공통
def referer_for(url: str) -> str:
    host = urlparse(url).netloc
    if "dcinside" in host or "dcimg" in host:
        return "https://gall.dcinside.com/"
    if "ruliweb" in host:
        return "https://bbs.ruliweb.com/"
    return f"https://{host}/"


def fetch_image(url: str) -> tuple[bytes, str] | None:
    try:
        r = http.get(url, headers={"Referer": referer_for(url)}, timeout=20)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.content:
        return None
    ctype = r.headers.get("Content-Type", "").split(";")[0] or mimetypes.guess_type(url)[0] or "image/jpeg"
    return r.content, ctype


def ext_for(ctype: str) -> str:
    return {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
            "video/mp4": ".mp4"}.get(ctype, ".jpg")


def week_label(d: datetime) -> tuple[int, int]:
    """그 주의 목요일이 속한 달 기준 주차 (11/2 월요일 → 11월 1주 차)."""
    thursday = (d - timedelta(days=d.weekday())) + timedelta(days=3)
    return thursday.month, (thursday.day - 1) // 7 + 1


def clean_heading(title: str) -> str:
    """가운데 들어갈 작은 제목: '…' 앞뒤 중 브랜드가 들어간 쪽을 고름."""
    t = re.sub(r"\[[^\]]*\]|【[^】]*】", "", title)
    parts = [p.strip(" -·,") for p in re.split(r"…|\.\.\.", t) if p.strip(" -·,")]
    if not parts:
        return title[:40]
    pick = next((p for p in parts if re.search(r"[가-힣A-Za-z0-9]{2,}\s*,", p)), parts[0])
    return pick[:40]


def default_comment(summary: str) -> str:
    """기사/게임 설명에서 코멘트 초안. 문장을 자르지 않고, 끝이 잘린(...) 문장은 뺌."""
    s = re.sub(r"\s+", " ", html.unescape(summary or "")).strip()
    s = re.sub(r"^[\[(【]?[^\])】]{0,20}[\])】]?\s*[가-힣]{2,4}\s*기자\s*=?\s*", "", s)  # [서울=뉴시스] 홍길동 기자 =
    s = re.sub(r"^.{0,30}?기자\s*=\s*", "", s)
    sents = [x.strip() for x in re.split(r"(?<=[.!?。])\s+", s) if x.strip()]
    full = [x for x in sents if not re.search(r"(\.\.\.|…|\.\.)$", x)]
    if not full and sents:  # 전부 잘린 문장뿐이면 마지막 완결 지점까지만
        cut = re.split(r"(?<=[다요])[,\s]", sents[0].rstrip(".… "))
        full = [cut[0]] if cut else []
    lines = []
    for x in full[:3]:
        x = x.rstrip(".")
        if len(x) > 55 and ", " in x:  # 긴 문장은 쉼표에서 줄만 바꿈
            head, tail = x.split(", ", 1)
            lines += [head + ",", tail]
        else:
            lines.append(x)
    return "\n".join(lines[:4])


def items_for(category: str, show_used: bool) -> list[sqlite3.Row]:
    cfg = load_config()
    since = (datetime.now(KST) - timedelta(days=cfg.get("days", 7))).isoformat()
    con = db()
    q = "SELECT rowid AS id, * FROM items WHERE category=? AND posted_at>=?"
    q += " AND used IN (0,1)" if show_used else " AND used=0"  # 2 = 숨김
    q += " ORDER BY score DESC, posted_at DESC LIMIT 150"
    rows = con.execute(q, (category, since)).fetchall()
    con.close()
    if category == "유머":
        bad = cfg.get("humor_exclude_words", [])
        rows = [r for r in rows if not is_controversial(r["title"], bad)]
    return rows


def open_folder(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ---------------------------------------------------------------- 화면
LAYOUT = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ page_title }}</title>
<style>
:root{--bg:#141414;--panel:#1e1e1e;--line:#333;--text:#e8e8e8;--muted:#9a9a9a;--accent:#03c75a;--warn:#ffb020}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 'Malgun Gothic',system-ui,sans-serif}
a{color:#7cc4ff} header{position:sticky;top:0;z-index:5;background:#0d0d0d;border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
header b{margin-right:12px} .tab{padding:6px 14px;border-radius:999px;background:var(--panel);color:var(--text);text-decoration:none;border:1px solid var(--line)}
.tab.on{background:var(--accent);color:#000;border-color:var(--accent);font-weight:700}
main{max-width:1100px;margin:0 auto;padding:16px}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0 16px}
button,.btn{background:var(--accent);color:#000;border:0;border-radius:8px;padding:9px 16px;font-weight:700;cursor:pointer;text-decoration:none;font-size:14px}
button.ghost,.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px;display:grid;grid-template-columns:28px 180px 1fr;gap:12px}
.card.sel{border-color:var(--accent)}
.card img,.card video{width:180px;height:130px;object-fit:cover;border-radius:8px;background:#000}
.meta{color:var(--muted);font-size:13px} .title{font-weight:700;margin:2px 0 6px}
input[type=text],textarea{width:100%;background:#111;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
textarea{min-height:70px} .thumbs{display:flex;gap:4px;flex-wrap:wrap}
.thumbs img{width:56px;height:56px} .empty{color:var(--muted);padding:40px;text-align:center}
@media (max-width:700px){.card{grid-template-columns:24px 1fr}.card .pic{grid-column:2}.card img{width:100%;height:auto}}
.preview{background:#000;border:1px solid var(--line);border-radius:12px;padding:24px;max-width:860px}
.note{background:#2a2410;border:1px solid #5a4a14;color:#ffd77a;border-radius:8px;padding:10px 14px;margin:10px 0;font-size:14px}
</style></head><body>
<header><b>주간 게시글 도우미</b>
{% for c in cats %}<a class="tab {{ 'on' if c==cat else '' }}" href="{{ url_for('index', cat=c) }}">{{ c }}</a>{% endfor %}
</header><main>{{ body|safe }}</main></body></html>"""

LIST = """
<div class="bar">
  <span class="meta">최근 {{ days }}일 · {{ rows|length }}개 · 점수 높은 순 {% if not show_used %}(이미 쓴 것 숨김){% endif %}</span>
  <a class="btn ghost" href="?cat={{ cat }}&used={{ 0 if show_used else 1 }}">{{ '쓴 것 숨기기' if show_used else '쓴 것도 보기' }}</a>
</div>
{% if not rows %}<div class="empty">아직 모인 자료가 없어요. 먼저 <b>1_수집하기.bat</b>을 실행해 주세요.</div>{% endif %}
{% if roundup %}
<form method="post" action="{{ url_for('build_roundup', cat=cat) }}">
<div class="bar"><button>체크한 것으로 글 만들기</button><span class="meta">제목줄·코멘트는 여기서 미리 고쳐도 되고, 완성 글에서 고쳐도 돼요.</span></div>
{% endif %}
{% for r in rows %}{% set imgs = r['images']|fromjson %}
<div class="card" id="c{{ r['id'] }}">
  <div>{% if roundup %}<input type="checkbox" name="pick" value="{{ r['id'] }}" onchange="this.closest('.card').classList.toggle('sel',this.checked)">{% endif %}</div>
  <div class="pic">{% if imgs %}{% if imgs[0].endswith('.mp4') %}<video src="/img?u={{ imgs[0]|urlencode }}" muted></video>{% else %}<img loading="lazy" src="/img?u={{ imgs[0]|urlencode }}">{% endif %}
       {% if imgs|length > 1 %}<div class="meta">+{{ imgs|length - 1 }}장</div>{% endif %}{% else %}<div class="meta">이미지 없음</div>{% endif %}</div>
  <div>
    <div class="meta">{{ r['source'] }} · {{ r['posted_at'][:16].replace('T',' ') }}
      {% if r['views'] %} · 조회 {{ '{:,}'.format(r['views']) }}{% endif %}{% if r['likes'] %} · 추천 {{ '{:,}'.format(r['likes']) }}{% endif %}{% if r['comments'] %} · 댓글 {{ r['comments'] }}{% endif %}
      · 점수 {{ r['score'] }} · <a href="{{ r['url'] }}" target="_blank" rel="noopener">원본</a>{% if r['used'] %} · <b style="color:var(--warn)">사용함</b>{% endif %}
      · <a href="#" onclick="hideItem({{ r['id'] }});return false" style="color:var(--muted)">숨기기</a></div>
    <div class="title">{{ r['title'] }}</div>
    {% if roundup %}
      <input type="text" name="h{{ r['id'] }}" value="{{ r['title'] if r['source'].startswith(('스팀','itch')) else clean(r['title']) }}" placeholder="가운데 들어갈 작은 제목">
      <textarea name="t{{ r['id'] }}" placeholder="코멘트 2~4줄">{{ comment(r['summary']) }}</textarea>
      {% if imgs|length > 1 %}<div class="meta">넣을 이미지:</div><div class="thumbs">{% for u in imgs %}
        <label><input type="checkbox" name="i{{ r['id'] }}" value="{{ loop.index0 }}" {{ 'checked' if loop.index0 < 1 else '' }}><img loading="lazy" src="/img?u={{ u|urlencode }}"></label>{% endfor %}</div>{% endif %}
    {% else %}
      <form method="post" action="{{ url_for('build_humor', item_id=r['id']) }}" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input type="text" name="title" value="{{ r['title'] }}" style="flex:1;min-width:220px">
        <button>이 짤로 글 만들기</button>
      </form>
      {% if imgs|length > 1 %}<div class="thumbs" style="margin-top:6px">{% for u in imgs[1:6] %}<img loading="lazy" src="/img?u={{ u|urlencode }}">{% endfor %}</div>{% endif %}
    {% endif %}
  </div>
</div>{% endfor %}
{% if roundup %}{% if rows %}<div class="bar"><button>체크한 것으로 글 만들기</button></div>{% endif %}</form>{% endif %}
<script>function hideItem(id){fetch('/hide/'+id,{method:'POST'}).then(()=>document.getElementById('c'+id).remove())}</script>
"""

PREVIEW = """
<div class="bar">
  <button onclick="copyPost()">📋 전체 복사 (글+이미지)</button>
  <button class="ghost" onclick="copyTitle()">제목 복사</button>
  <form method="post" action="{{ url_for('open_output') }}" style="display:inline"><input type="hidden" name="dir" value="{{ folder }}"><button class="ghost">이미지 폴더 열기</button></form>
  <a class="btn ghost" href="{{ url_for('index', cat=cat) }}">← 목록으로</a>
  <span id="msg" class="meta"></span>
</div>
<div class="note">카페 글쓰기 창에 제목을 붙여 넣고, 본문 칸을 클릭한 뒤 Ctrl+V 하세요.
이미지가 안 들어가면 <b>이미지 폴더 열기</b>로 번호 순서대로 저장된 사진을 한 번에 올리면 돼요.
글 안에서 바로 고쳐도 되고(아래 미리보기는 수정 가능), 카페에서 고쳐도 돼요.</div>
<p><b>제목</b> <input id="ptitle" type="text" value="{{ title }}"></p>
<div class="preview" id="post" contenteditable="true">{{ body|safe }}</div>
<p class="meta">저장 위치: {{ folder }}</p>
<script>
function flash(t){const m=document.getElementById('msg');m.textContent=t;setTimeout(()=>m.textContent='',3000)}
async function copyPost(){
  const el=document.getElementById('post');
  try{
    const html=el.innerHTML, text=el.innerText;
    await navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([html],{type:'text/html'}),'text/plain':new Blob([text],{type:'text/plain'})})]);
    flash('복사됐어요! 카페 본문에 Ctrl+V');
  }catch(e){
    const r=document.createRange();r.selectNodeContents(el);const s=getSelection();s.removeAllRanges();s.addRange(r);
    document.execCommand('copy');s.removeAllRanges();flash('복사됐어요! 카페 본문에 Ctrl+V');
  }
}
function copyTitle(){navigator.clipboard.writeText(document.getElementById('ptitle').value);flash('제목 복사됨')}
</script>
"""


def page(cat: str, body: str, title: str = "주간 게시글 도우미") -> str:
    cats = list(load_config().get("categories", {}).keys()) + ["유머"]
    return render_template_string(LAYOUT, cats=cats, cat=cat, body=body, page_title=title)


@app.template_filter("fromjson")
def _fromjson(s):
    try:
        return json.loads(s or "[]")
    except Exception:
        return []


@app.route("/")
def index():
    cfg = load_config()
    cat = request.args.get("cat") or next(iter(cfg["categories"]), "유머")
    show_used = request.args.get("used") == "1"
    rows = items_for(cat, show_used)
    body = render_template_string(LIST, rows=rows, cat=cat, roundup=(cat != "유머"),
                                  days=cfg.get("days", 7), show_used=show_used,
                                  clean=clean_heading, comment=default_comment)
    return page(cat, body)


@app.route("/img")
def img_proxy():
    u = request.args.get("u", "")
    if not u.startswith("http"):
        abort(400)
    got = fetch_image(u)
    if not got:
        abort(404)
    data, ctype = got
    return Response(data, mimetype=ctype, headers={"Cache-Control": "max-age=86400"})


def save_and_embed(urls: list[str], folder: Path, start: int) -> tuple[list[str], int]:
    """이미지를 번호 붙여 저장하고, 붙여넣기용 <img>(base64 내장) 태그를 돌려줍니다."""
    tags, n = [], start
    for u in urls:
        got = fetch_image(u)
        if not got:
            continue
        data, ctype = got
        n += 1
        (folder / f"{n:02d}{ext_for(ctype)}").write_bytes(data)
        if ctype.startswith("video/"):
            tags.append(f'<p style="text-align:center;color:#999">[영상 {n:02d}{ext_for(ctype)} 첨부]</p>')
            continue
        b64 = base64.b64encode(data).decode()
        tags.append(f'<p style="text-align:center"><img src="data:{ctype};base64,{b64}" '
                    f'style="max-width:100%;width:640px"></p>')
    return tags, n


def lines_html(text: str) -> str:
    rows = [html.escape(x) for x in (text or "").splitlines()]
    return "".join(f"<p>{x if x else '&nbsp;'}</p>" for x in rows)


def make_folder(name: str) -> Path:
    stamp = datetime.now(KST).strftime("%Y-%m-%d_%H%M")
    safe = re.sub(r'[\\/:*?"<>|]', "", name)[:40].strip() or "post"
    f = OUT / f"{stamp}_{safe}"
    f.mkdir(parents=True, exist_ok=True)
    return f


def finish(cat: str, title: str, body: str, folder: Path, ids: list[int]) -> str:
    (folder / "글.html").write_text(
        f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"<h3>{html.escape(title)}</h3>{body}", encoding="utf-8")
    (folder / "제목.txt").write_text(title, encoding="utf-8")
    con = db()
    con.executemany("UPDATE items SET used=1 WHERE rowid=?", [(i,) for i in ids])
    con.commit()
    con.close()
    preview = render_template_string(PREVIEW, title=title, body=body, folder=str(folder), cat=cat)
    return page(cat, preview, title)


@app.route("/build/<cat>", methods=["POST"])
def build_roundup(cat: str):
    cfg = load_config()
    conf = cfg["categories"].get(cat)
    if not conf:
        abort(404)
    ids = [int(x) for x in request.form.getlist("pick")]
    if not ids:
        return redirect(url_for("index", cat=cat))
    con = db()
    rows = {r["id"]: r for r in con.execute(
        f"SELECT rowid AS id, * FROM items WHERE rowid IN ({','.join('?' * len(ids))})", ids)}
    con.close()
    month, week = week_label(datetime.now(KST))
    title = conf["title"].format(month=month, week=week)
    folder = make_folder(title)
    parts = [lines_html(conf.get("intro", "")), "<p>&nbsp;</p>"]
    n = k = 0
    for i in ids:  # 화면에 보이던 순서(점수순) 그대로
        r = rows.get(i)
        if not r:
            continue
        imgs = json.loads(r["images"] or "[]")
        chosen = request.form.getlist(f"i{i}")
        if chosen:
            imgs = [imgs[int(k)] for k in chosen if int(k) < len(imgs)]
        else:
            imgs = imgs[:1]
        heading = request.form.get(f"h{i}") or clean_heading(r["title"])
        comment = request.form.get(f"t{i}", "")
        if conf.get("style") == "numbered":  # 게임 추천: 번호. 이름 → 링크 → 사진 → 설명
            k += 1
            link = html.escape(r["url"])
            parts.append(f"<p><b>{k}. {html.escape(heading)}</b></p>"
                         f'<p><a href="{link}">{link}</a></p>')
        else:  # 신상/애니: 가운데 작은 제목 → 사진 → 코멘트
            parts.append(f'<p style="text-align:center"><b><i><span style="font-size:13px">'
                         f'{html.escape(heading)}</span></i></b></p>')
        tags, n = save_and_embed(imgs, folder, n)
        parts += tags
        parts.append(lines_html(comment))
        parts.append("<p>&nbsp;</p><p>&nbsp;</p>")
    return finish(cat, title, "".join(parts), folder, ids)


@app.route("/humor/<int:item_id>", methods=["POST"])
def build_humor(item_id: int):
    con = db()
    r = con.execute("SELECT rowid AS id, * FROM items WHERE rowid=?", (item_id,)).fetchone()
    con.close()
    if not r:
        abort(404)
    title = request.form.get("title") or r["title"]
    folder = make_folder(title)
    tags, _ = save_and_embed(json.loads(r["images"] or "[]"), folder, 0)
    body = "".join(tags) + "<p>&nbsp;</p>" + \
        f'<p style="color:#888;font-size:12px">출처: {html.escape(r["source"])}</p>'
    return finish("유머", title, body, folder, [item_id])


@app.route("/hide/<int:item_id>", methods=["POST"])
def hide(item_id: int):
    con = db()
    con.execute("UPDATE items SET used=2 WHERE rowid=?", (item_id,))
    con.commit()
    con.close()
    return ("", 204)


@app.route("/open", methods=["POST"])
def open_output():
    d = Path(request.form.get("dir", ""))
    if d.is_dir() and OUT in d.resolve().parents:
        open_folder(d)
    return ("", 204)


if __name__ == "__main__":
    if not DB_PATH.exists():
        db().close()
    port = int(os.environ.get("PORT", "5000"))
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"고르기 화면: http://127.0.0.1:{port}  (끄려면 이 창에서 Ctrl+C)")
    app.run(host="127.0.0.1", port=port, debug=False)
