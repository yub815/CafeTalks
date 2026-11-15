// 게임 검색용 Cloudflare Worker (무료).
// 사이트(브라우저)는 스팀에 바로 검색을 못 보내서(스팀이 막음), 이 작은 중계 서버가 대신 스팀에 물어봐요.
// 스팀 검색·게임 정보 두 가지만 중계하고, 사이트 주소에서 온 요청만 받아요.
const ALLOW = ["https://yub815.github.io"];

export default {
  async fetch(req) {
    const origin = req.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": ALLOW.includes(origin) ? origin : ALLOW[0],
      "Access-Control-Allow-Methods": "GET",
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
      return json({ ok: true, use: "/search?q=이름, /app?id=번호" });
    } catch (e) {
      return json({ error: String(e) }, 502);
    }
  },
};
