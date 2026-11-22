import os, requests, json
T=os.environ["IG_ACCESS_TOKEN"].strip(); A="https://graph.facebook.com/v21.0"
def g(p,**k):
    r=requests.get(f"{A}/{p}",params={**k,"access_token":T},timeout=30); return r.status_code, r.json()
import collector, instagram_source as I
for it in I.collect(collector.load_config()): print("ITEM",it["source"],it["posted_at"][:10],len(it["images"]),it["title"])
print("debug:", g("me", fields="id,name"))
s,d=g("me/accounts", fields="name,instagram_business_account"); print("accounts:", s, json.dumps(d)[:400])
ig=next((p["instagram_business_account"]["id"] for p in d.get("data",[]) if p.get("instagram_business_account")),None)
print("ig:", bool(ig))
C="""orion_korea orionkorea orion.official orion_kr crownconfec crown_confectionery crownmall haitai haitaifood haitai_kr binggrae_kr binggraeofficial binggrae.official
ottogi_kr ottogimall ottogi.official lotteria_kr lotteriakorea momstouch momstouch_kr kfc_korea kfckorea_official dominos_kr dominoskorea megacoffee megacoffee_kr
composecoffee compose_coffee paikdabang paikdabang_official dunkin_kr dunkindonuts_kr bhc_chicken bbq_chicken_official kyochon_official goobne_official
lottemart_official emart_official homeplus_official nobrand_official pepero_official chilsung_cider coca_cola_korea pepsi_korea lotte_chilsung
haagendazs_kr gongcha_korea twosome_place twosomeplace hollys_coffee""".split()
for u in C:
    s,d=g(ig, fields=f"business_discovery.username({u}){{username,name,followers_count,media.limit(2){{timestamp,caption}}}}")
    if s!=200: print("X",u,(d.get("error") or {}).get("message","")[:80]); continue
    b=d["business_discovery"]; m=(b.get("media") or {}).get("data",[])
    print("O",u,b.get("name"),b.get("followers_count"), m[0]["timestamp"][:10] if m else "-", (m[0].get("caption") or "")[:50].replace("\n"," ") if m else "")
