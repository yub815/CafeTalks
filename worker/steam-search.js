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
      if (url.pathname === "/ping") return new Response("ok auto-deploy 2");  // 자동 배포 확인용
      if (url.pathname === "/search") {
        const q = (url.searchParams.get("q") || "").slice(0, 80);
        if (!q) return json({ items: [] });
        const r = await fetch(`https://store.steampowered.com/api/storesearch/?term=${encodeURIComponent(q)}&l=koreana&cc=KR`);
        const d = await r.json();
        return json({
          items: (d.items || []).filter(x => x.type === "app").slice(0, 10).map(x => ({
            id: x.id, name: x.name,
            img: x.tiny_image || `https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/${x.id}/header.jpg`,
          })),
        });
      }
      if (url.pathname === "/app") {
        const id = (url.searchParams.get("id") || "").replace(/\D/g, "");
        if (!id) return json({ error: "id" }, 400);
        const r = await fetch(`https://store.steampowered.com/api/appdetails?appids=${id}&l=koreana&cc=KR`);
        const d = ((await r.json())[id] || {}).data || {};
        // 설명란 움짤 (스팀이 GIF를 mp4로 바꿔 둠) → 사이트에서 GIF로 바꿔서 받음
        const html = d.about_the_game || d.detailed_description || "";
        const videos = [...new Set([...html.matchAll(/src="(https:\/\/[^"]+\/extras\/[^"?]+\.mp4)/g)].map(m => m[1]))].slice(0, 2);
        return json({
          id, name: d.name || "",
          desc: (d.short_description || "").replace(/<[^>]+>/g, "").trim(),
          genres: (d.genres || []).map(g => g.description),
          images: (d.screenshots || []).slice(0, 4).map(s => s.path_full).concat(d.header_image ? [] : []),
          header: d.header_image || "",
          videos,
          url: `https://store.steampowered.com/app/${id}/`,
        });
      }
      // GIF 보관: 사이트가 브라우저에서 만든 GIF를 올려두면, 카페 글에 이 주소로 붙여 넣어요
      //   /gif/<이름>.gif  (이름 = 스팀 게임번호_파일이름)
      if (url.pathname.startsWith("/gif/")) {
        const key = url.pathname.slice(5).replace(/\.gif$/, "");
        if (!/^\d+_[\w-]{1,80}$/.test(key)) return json({ error: "key" }, 400);
        if (!env.GIFS) return json({ error: "no storage" }, 500);
        if (req.method === "POST") {
          if (!ALLOW.includes(origin)) return json({ error: "origin" }, 403);
          const buf = await req.arrayBuffer();
          const head = new Uint8Array(buf.slice(0, 6));
          if (buf.byteLength > 20e6 || String.fromCharCode(...head) !== "GIF89a") return json({ error: "gif" }, 400);
          await env.GIFS.put(key, buf);
          return json({ ok: true });
        }
        const g = await env.GIFS.get(key, "arrayBuffer");
        if (!g) return new Response("not found", { status: 404, headers: cors });
        return new Response(g, { headers: { ...cors, "Content-Type": "image/gif", "Cache-Control": "public, max-age=31536000" } });
      }
      if (url.pathname === "/media") {  // 스팀 움짤 파일 중계 (사이트에서 GIF로 바꾸려고)
        const u = new URL(url.searchParams.get("u") || "");
        if (!u.hostname.endsWith("steamstatic.com")) return json({ error: "host" }, 400);
        const r = await fetch(u.toString());
        return new Response(r.body, { status: r.status, headers: { ...cors, "Content-Type": r.headers.get("Content-Type") || "video/mp4" } });
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
