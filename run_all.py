"""One command: scrape + analyze everyone in people.csv, run the dating round, save the finished example.

    python run_all.py            (site is live at http://localhost:5000 while it runs - open /live to watch)
"""
import csv
import os
import shutil
import sys
import threading
import time

import engine
import scrapers
import store
from app import app

CSV = sys.argv[1] if len(sys.argv) > 1 else "people.csv"


def serve():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), threaded=True, use_reloader=False)


def wait_people():
    while True:
        ps = store.snapshot("people")
        busy = [p for p in ps.values() if p["status"] in ("queued", "scraping", "analyzing")]
        ready = [p for p in ps.values() if p["status"] == "ready"]
        err = [p for p in ps.values() if p["status"] == "error"]
        print(f"people: {len(ready)} ready, {len(busy)} working, {len(err)} failed")
        if not busy:
            for p in err:
                print("  FAILED", p["instagram"], "->", p.get("step"))
            return
        time.sleep(10)


def main():
    threading.Thread(target=serve, daemon=True).start()
    rows = []
    with open(CSV, newline="", encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            li = next((x.strip() for x in r if "linkedin.com" in x), None)
            ig = next((x.strip() for x in r if "instagram.com" in x), None)
            if li and ig:
                rows.append((li, ig))
    print(f"{len(rows)} people. Open http://localhost:5000/live to watch.")
    # restart-safe: drop people/dates that were interrupted, retry them
    wanted = {(li.rstrip("/").lower(), ig.rstrip("/").lower()) for li, ig in rows}
    store.mutate(lambda db: [db["people"].pop(k) for k, p in list(db["people"].items()) if p["status"] != "ready"
                             or (p.get("demo") or p.get("created", 0) < 1790797000) and
                             (p["linkedin"].rstrip("/").lower(), p["instagram"].rstrip("/").lower()) not in wanted])
    live_ids = set(store.snapshot("people"))
    store.mutate(lambda db: [db["dates"].pop(k) for k, d in list(db["dates"].items()) if d["a"] not in live_ids or d["b"] not in live_ids])
    store.mutate(lambda db: [db["scores"].pop(k) for k in list(db["scores"]) if k not in live_ids])
    store.mutate(lambda db: [db["dates"].pop(k) for k, d in list(db["dates"].items())
                             if d["status"] in ("asking", "on_date", "debrief", "error")])
    store.mutate(lambda db: db.__setitem__("round", {"status": "idle"}))
    igs = {ig.rstrip("/").lower() for _, ig in rows}
    store.mutate(lambda db: [p.__setitem__("demo", True) for p in db["people"].values()
                             if p["instagram"].rstrip("/").lower() in igs])
    # fresh feed for the recording (old errors mentioned real handles)
    store.mutate(lambda db: db.__setitem__("events", []))
    todo = [(li, ig) for li, ig in rows if not any(
        p["instagram"].rstrip("/") == ig.rstrip("/") and p["status"] == "ready" for p in store.snapshot("people").values())]
    print(f"{len(rows) - len(todo)} already done, {len(todo)} to process")
    if todo:
        print("Batch scraping with Apify (2 runs, a few minutes)…")
        scrapers.prefetch(todo)
        for li, ig in todo:
            pid = engine.add_person(li, ig)
            store.mutate(lambda db, pid=pid: db["people"][pid].__setitem__("demo", True))
        wait_people()
    # people who haven't been on a date yet get their own quick round (much faster than re-running everyone)
    dated = set()
    for d in store.snapshot("dates").values():
        if d["status"] == "done":
            dated.update([d["a"], d["b"]])
    undated = [pid for pid, p in store.snapshot("people").items() if p["status"] == "ready" and pid not in dated]
    if undated and len(undated) < len(rows) // 2 and os.getenv("FULL_ROUND") != "1":
        print(f"{len(undated)} agents have not dated yet - sending them out now")
        store.mutate(lambda db: db.__setitem__("round", {"status": "dating"}))
        store.event("round", f"🥂 {len(undated)} agents who haven't dated yet enter the mixer")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(engine.date_newcomer, undated))
        store.mutate(lambda db: db.__setitem__("round", {"status": "done"}))
        store.event("round", "🏁 Round finished — rankings updated")
    elif os.getenv("SKIP_ROUND") != "1":
        engine.dating_round()
        time.sleep(2)
        while store.snapshot("round").get("status") not in ("done", "idle"):
            dates = store.snapshot("dates").values()
            print(f"round: {store.snapshot('round').get('status')} | dates done {sum(d['status'] == 'done' for d in dates)}"
                  f" / {len(list(dates))}")
            time.sleep(15)
    store.save()
    os.makedirs("seed", exist_ok=True)
    import subprocess
    subprocess.run([sys.executable, "make_public_seed.py"], check=False)  # public, pseudonymised copy
    print("Saved finished example to seed/demo.json. Site still running at http://localhost:5000 (Ctrl+C to stop).")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
