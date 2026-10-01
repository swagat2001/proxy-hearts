import os
import re
import time
from urllib.parse import urlparse

import requests
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

import engine
import scrapers
import store

app = Flask(__name__)
store.load()
IMG_HOSTS = ("cdninstagram.com", "fbcdn.net", "licdn.com", "media.licdn.com")
PRIVACY = os.getenv("PRIVACY_MODE", "0") == "1"
_mask = {"t": 0, "pairs": []}


def _mask_pairs():
    """Real name / handle -> pseudonym, for the demo people only (people a visitor adds themselves are not masked)."""
    if _mask.get("v") == store.VERSION[0]:
        return _mask["pairs"]
    people = sorted(store.read("people").values(), key=lambda p: p.get("created", 0))
    pairs, firsts = [], {}
    for i, p in enumerate([p for p in people if p.get("demo")], 1):
        alias = f"Person {i:02d}"
        names = {p.get("name"), (p.get("analysis") or {}).get("name"), (p.get("li") or {}).get("fullName"),
                 (p.get("ig") or {}).get("fullName")}
        for n in [n for n in names if n and len(n) > 2 and not re.match(r"^Person \d\d$", n.strip())]:
            n = " ".join(n.split())
            pairs.append((n, alias))
            parts = n.split()
            if len(parts) > 1:
                pairs.append((" ".join(parts[:2]), alias))
                if len(parts[-1]) > 3:
                    pairs.append((parts[-1], alias))
                firsts.setdefault(parts[0], set()).add(alias)
        for h in [((p.get("ig") or {}).get("username")), p.get("instagram", "").rstrip("/").rsplit("/", 1)[-1],
                  p.get("linkedin", "").rstrip("/").rsplit("/", 1)[-1]]:
            if h and len(h) > 3 and h != "hidden":
                pairs.append(("@" + h, alias))
                pairs.append((h, alias))
    for f, al in firsts.items():
        if len(f) > 2:
            pairs.append((f, al.pop() if len(al) == 1 else "they"))
    pairs.sort(key=lambda x: -len(x[0]))
    _mask.update(t=time.time(), v=store.VERSION[0], pairs=pairs)
    return pairs


_rx = {"v": None, "rx": None, "map": {}}


def mask_text(s):
    if _rx["v"] != store.VERSION[0] or _rx["rx"] is None:
        pairs = _mask_pairs()
        m = {}
        for real, alias in pairs:
            m.setdefault(real.lower(), alias)
        _rx.update(v=store.VERSION[0], map=m, rx=re.compile(
            r"(?<![\w])(" + "|".join(re.escape(r) for r, _ in pairs) + r")(?![\w])", re.I) if pairs else None)
    if _rx["rx"] is not None:
        s = _rx["rx"].sub(lambda mo: _rx["map"].get(mo.group(0).lower(), mo.group(0)), s)
    # drop flag/emoji left over from names like "Name 🇮🇳"
    return re.sub(r"(Person \d\d)\s*[\U0001F1E6-\U0001F1FF\u2600-\u27BF\U0001F300-\U0001FAFF]+", r"\1", s)


@app.after_request
def _privacy(resp):
    if PRIVACY and resp.mimetype in ("text/html", "application/json") and not resp.direct_passthrough:
        resp.set_data(mask_text(resp.get_data(as_text=True)))
    return resp


@app.template_filter("img")
def img_filter(u):
    if u and u.startswith("data:"):
        return u
    return url_for("img", u=u) if u else url_for("static", filename="avatar.svg")


@app.route("/")
def index():
    db = store.read()
    people = sorted(db["people"].values(), key=lambda p: p.get("created", 0))
    done = [d for d in db["dates"].values() if d["status"] == "done"]
    return render_template("index.html", people=people, round=db["round"], n_dates=len(done))


@app.post("/add")
def add():
    li, ig = request.form.get("linkedin", ""), request.form.get("instagram", "")
    try:
        pid = engine.add_person(li, ig, auto_date=request.form.get("auto_date") == "on")
    except scrapers.ScrapeError as e:
        return render_template("error.html", msg=str(e)), 400
    return redirect(url_for("person", pid=pid))


@app.post("/bulk")
def bulk():
    n = 0
    for line in request.form.get("lines", "").splitlines():
        parts = [x.strip() for x in line.replace("\t", ",").split(",") if x.strip()]
        li = next((x for x in parts if "linkedin.com" in x), None)
        ig = next((x for x in parts if "instagram.com" in x), None)
        if li and ig:
            try:
                engine.add_person(li, ig)
                n += 1
            except scrapers.ScrapeError:
                pass
    return redirect(url_for("index", added=n))


@app.post("/round")
def start_round():
    engine.dating_round()
    return redirect(url_for("live"))


@app.post("/person/<pid>/date")
def person_date(pid):
    p = store.snapshot("people").get(pid)
    if not p or p.get("status") != "ready":
        abort(404)
    import threading
    threading.Thread(target=engine.date_newcomer, args=(pid,), daemon=True).start()
    return redirect(url_for("live"))


@app.route("/person/<pid>")
def person(pid):
    p = store.read("people").get(pid) or abort(404)
    dates = [d for d in store.read("dates").values() if pid in (d["a"], d["b"])]
    dates.sort(key=lambda d: d.get("created", 0))
    ranking = engine.ranking_for(pid) if p.get("status") == "ready" else []
    return render_template("person.html", p=p, a=p.get("analysis") or {}, dates=dates, ranking=ranking)


@app.route("/date/<did>")
def date(did):
    d = store.read("dates").get(did) or abort(404)
    people = store.read("people")
    return render_template("date.html", d=d, A=people[d["a"]], B=people[d["b"]])


@app.route("/live")
def live():
    return render_template("live.html")


@app.route("/rankings")
def rankings():
    people = [p for p in store.read("people").values() if p.get("status") == "ready"]
    people.sort(key=lambda p: p["analysis"]["name"])
    return render_template("rankings.html", rows=[(p, engine.ranking_for(p["id"])[:5]) for p in people])


@app.route("/how")
def how():
    return render_template("how.html")


# ---------------- JSON API (used by the live pages) ----------------
@app.route("/api/status")
def api_status():
    db = store.snapshot()
    return jsonify(round=db["round"], people={k: {"status": v["status"], "step": v.get("step"), "name": v.get("name")}
                                              for k, v in db["people"].items()})


@app.route("/api/events")
def api_events():
    since = int(request.args.get("since", 0))
    db = store.read()
    active = [{"id": d["id"], "a_name": d["a_name"], "b_name": d["b_name"], "status": d["status"],
               "venue": (d.get("plan") or {}).get("venue"), "last": (d["messages"][-1]["text"] if d["messages"] else "")}
              for d in db["dates"].values() if d["status"] in ("asking", "on_date", "debrief")]
    return jsonify(events=[e for e in db["events"] if e["n"] > since][-200:], active=active, round=db["round"],
                   done=sum(1 for d in db["dates"].values() if d["status"] == "done"))


@app.route("/api/date/<did>")
def api_date(did):
    return jsonify(store.snapshot("dates").get(did) or {})


@app.route("/api/person/<pid>")
def api_person(pid):
    p = store.snapshot("people").get(pid) or {}
    if PRIVACY and p.get("demo"):
        p = {k: v for k, v in p.items() if k not in ("li", "ig", "photo", "linkedin", "instagram")}
    return jsonify(p)


@app.route("/api/export")
def export():
    if PRIVACY:
        abort(404)
    store.save()
    return Response(open(store.DATA, encoding="utf-8").read(), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=demo.json"})


@app.route("/img")
def img():
    u = request.args.get("u", "")
    host = urlparse(u).hostname or ""
    if not any(host.endswith(h) for h in IMG_HOSTS):
        abort(400)
    try:
        r = requests.get(u, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:  # noqa: BLE001
        return redirect(url_for("static", filename="avatar.svg"))


@app.context_processor
def _ctx():
    def pic(p):
        if not p or (PRIVACY and p.get("demo")) or not p.get("photo"):
            return url_for("static", filename="avatar.svg")
        return img_filter(p.get("photo"))
    return {"now": time.time(), "privacy": PRIVACY, "pic": pic}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False, threaded=True)
