import os, requests, json
T=os.environ["IG_ACCESS_TOKEN"].strip(); A="https://graph.facebook.com/v21.0"
def g(p,**k):
    r=requests.get(f"{A}/{p}",params={**k,"access_token":T},timeout=30); return r.status_code, r.json()
print("debug:", g("me", fields="id,name"))
s,d=g("me/accounts", fields="name,instagram_business_account"); print("accounts:", s, json.dumps(d)[:400])
ig=next((p["instagram_business_account"]["id"] for p in d.get("data",[]) if p.get("instagram_business_account")),None)
print("ig:", bool(ig))
C="""cu_official bgfretail_cu cu_cvs gs25_official gs25official gs25 7eleven_korea 7elevenkorea seveneleven_korea emart24_official emart24
nongshim_official nongshim nongshimkorea orion_official orionworld orion_snack lottewellfood lotte_wellfood lotteconf haitai_official haitaiconfectionery
crown_official crownconfectionery binggrae_official binggrae ottogi_official ottogi samyangfoods samyang_official buldak_official paldo_official paldofood
pulmuone_official cj_cheiljedang cjcheiljedang starbuckskorea mcdonalds_kr burgerkingkorea lotteria_official lotteria baskinrobbinskorea baskinrobbins.kr dunkin_korea
parisbaguette_official parisbaguette_kr momstouch_official kfckorea dominos_korea ediya_official megacoffee_official compose_coffee_official paikdabang_official
oliveyoung_official daiso_official""".split()
for u in C:
    s,d=g(ig, fields=f"business_discovery.username({u}){{username,name,followers_count,media.limit(2){{timestamp,caption}}}}")
    if s!=200: print("X",u,(d.get("error") or {}).get("message","")[:80]); continue
    b=d["business_discovery"]; m=(b.get("media") or {}).get("data",[])
    print("O",u,b.get("name"),b.get("followers_count"), m[0]["timestamp"][:10] if m else "-", (m[0].get("caption") or "")[:50].replace("\n"," ") if m else "")
