"""Build seed/demo.json for the PUBLIC repo/deploy: pseudonyms instead of names, no raw scraped data, photos or links."""
import json
import re

db = json.load(open("data/store.json", encoding="utf-8"))
people = sorted(db["people"].values(), key=lambda p: p.get("created", 0))
pairs, firsts = [], {}
for i, p in enumerate([p for p in people if p.get("demo")], 1):
    alias = f"Person {i:02d}"
    names = {p.get("name"), (p.get("analysis") or {}).get("name"), (p.get("li") or {}).get("fullName"), (p.get("ig") or {}).get("fullName")}
    for n in [" ".join(n.split()) for n in names if n and len(n) > 2]:
        pairs.append((n, alias))
        parts = n.split()
        if len(parts) > 1:
            pairs.append((" ".join(parts[:2]), alias))
            if len(parts[-1]) > 3:
                pairs.append((parts[-1], alias))
            firsts.setdefault(parts[0], set()).add(alias)
    for h in [(p.get("ig") or {}).get("username"), p["instagram"].rstrip("/").rsplit("/", 1)[-1], p["linkedin"].rstrip("/").rsplit("/", 1)[-1]]:
        if h and len(h) > 3:
            pairs += [("@" + h, alias), (h, alias)]
for f, al in firsts.items():
    if len(f) > 2:
        pairs.append((f, al.pop() if len(al) == 1 else "they"))
pairs.sort(key=lambda x: -len(x[0]))

for p in db["people"].values():
    if not p.get("demo"):
        continue
    p.pop("photo", None)
    p["linkedin"], p["instagram"] = "https://www.linkedin.com/in/hidden", "https://www.instagram.com/hidden"
    if p.get("li"):
        p["li"] = {"fullName": p["li"].get("fullName"), "headline": None, "location": None}
    if p.get("ig"):
        p["ig"] = {"username": "hidden", "fullName": p["ig"].get("fullName"), "posts": [{} for _ in p["ig"].get("posts", [])]}

text = json.dumps(db, ensure_ascii=False)
for real, alias in pairs:
    text = re.sub(r"(?<![\w])" + re.escape(json.dumps(real)[1:-1]) + r"(?![\w])", alias, text, flags=re.I)
text = re.sub(r"(Person \d\d)\s*[\U0001F1E6-\U0001F1FF]+", r"\1", text)
out = json.loads(text)
json.dump(out, open("seed/demo.json", "w", encoding="utf-8"), ensure_ascii=False)
left = [r for r, _ in pairs if len(r) > 5 and re.search(r"(?<![\w])" + re.escape(r) + r"(?![\w])", text, re.I)]
print("public seed written:", len(out["people"]), "people,", sum(d["status"] == "done" for d in out["dates"].values()), "dates; leftover names:", left[:10])
