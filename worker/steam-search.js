// 게임 검색용 Cloudflare Worker (무료).
// 사이트(브라우저)는 스팀에 바로 검색을 못 보내서(스팀이 막음), 이 작은 중계 서버가 대신 스팀에 물어봐요.
// 스팀 검색·게임 정보를 중계하고, 사이트 주소에서 온 요청만 받아요.
// /write: 직접 추가한 게임의 카페 코멘트를 Claude(Haiku)로 써 줘요.
//   Settings → Variables and Secrets 에 ANTHROPIC_API_KEY (Secret) 를 넣어야 동작해요.
const ALLOW = ["https://yub815.github.io"];

export default {
  async fetch(req, env) {
    const origin = req.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": ALLOW.includes(origin) ? origin : ALLOW[0],
      "Access-Control-Allow-Methods": "GET, POST",
      "Access-Control-Allow-Headers": "Content-Type",
      "Content-Type": "application/json; charset=utf-8",
    };
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    const url = new URL(req.url);
    const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: cors });
    try {
      if (url.pathname === "/search") {
        const q = (url.searchParams.get("q") || "").slice(0, 80);
        if (!q) return json({ items: [] });
        const r = await fetch(`https://store.steampowered.com/api/storesearch/?term=${encodeURIComponent(q)}&l=koreana&cc=KR`);
        const d = await r.json();
        return json({
          items: (d.items || []).filter(x => x.type === "app").slice(0, 10).map(x => ({
            id: x.id, name: x.name,
            img: `https://cdn.akamai.steamstatic.com/steam/apps/${x.id}/header.jpg`,
          })),
        });
      }
      if (url.pathname === "/app") {
        const id = (url.searchParams.get("id") || "").replace(/\D/g, "");
        if (!id) return json({ error: "id" }, 400);
        const r = await fetch(`https://store.steampowered.com/api/appdetails?appids=${id}&l=koreana&cc=KR`);
        const d = ((await r.json())[id] || {}).data || {};
        return json({
          id, name: d.name || "",
          desc: (d.short_description || "").replace(/<[^>]+>/g, "").trim(),
          genres: (d.genres || []).map(g => g.description),
          images: (d.screenshots || []).slice(0, 4).map(s => s.path_full).concat(d.header_image ? [] : []),
          header: d.header_image || "",
          url: `https://store.steampowered.com/app/${id}/`,
        });
      }
      if (url.pathname === "/write" && req.method === "POST") {
        if (!ALLOW.includes(origin)) return json({ error: "origin" }, 403);
        if (!env.ANTHROPIC_API_KEY) return json({ error: "no key" }, 500);
        const b = await req.json();
        const name = String(b.name || "").slice(0, 100), desc = String(b.desc || "").slice(0, 1500);
        const prompt =
          "네이버 카페의 '게임 추천' 모음글에 들어갈 코멘트를 써 주세요.\n" +
          "- 2~4줄. 줄마다 짧게 끊어서 줄바꿈. 어떤 게임인지 + 뭘 하는 게임인지/재미 포인트 1~2개 + 가벼운 감상 한마디.\n" +
          "- 가볍고 짧은 구어체 다요체: 줄 끝을 '~다요'로 (게임이다요, 있다요, 추천한다요). '~요/~습니다'로 끝내지 마세요.\n" +
          "- 설명에 없는 사실은 지어내지 마세요. 마침표·이모지 없이. 코멘트만 출력하세요.\n\n" +
          "예시)\n'예' '아니오'로 답할 수 있는 질문을 하면 AI가 대답해주는 추리게임이다요\n" +
          "질문으로 단서 모아서 정답 제출하면 된다요\n질문 수를 줄일수록 등급이 높아지니 추천한다요\n\n" +
          `게임: ${name}\n설명: ${desc}`;
        const r = await fetch("https://api.anthropic.com/v1/messages", {
          method: "POST",
          headers: { "x-api-key": env.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json" },
          body: JSON.stringify({ model: "claude-haiku-5-5", max_tokens: 2000, output_config: { effort: "low" },
                                 messages: [{ role: "user", content: prompt }] }),
        });
        const d = await r.json();
        if (!r.ok) return json({ error: (d.error || {}).message || "api" }, 502);
        const text = (d.content || []).filter(c => c.type === "text").map(c => c.text).join("").trim();
        return json({ comment: text.replace(/다다요/g, "다요") });
      }
      return json({ ok: true, use: "/search?q=이름, /app?id=번호" });
    } catch (e) {
      return json({ error: String(e) }, 502);
    }
  },
};
