"""The harness: person pipeline, the mixer (agents choose who to ask out), live dates, reflections, rankings."""
import json
import os
import random
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import analyzer
import llm
import scrapers
import store

INVITES_PER_AGENT = int(os.getenv("INVITES_PER_AGENT", "3"))
DATE_TURNS = int(os.getenv("DATE_TURNS", "8"))          # total messages in a date
PARALLEL = int(os.getenv("PARALLEL_DATES", "3"))
_pool = ThreadPoolExecutor(max_workers=int(os.getenv("WORKERS", "4")))
_round_lock = threading.Lock()


def _set_person(pid, **kw):
    store.mutate(lambda db: db["people"][pid].update(kw))


def ready_people():
    return {k: v for k, v in store.snapshot("people").items() if v.get("status") == "ready"}


# ---------------------------------------------------------------- 1. person pipeline
def _photo_data(url):
    """Instagram/LinkedIn image URLs expire, so keep a small inline copy."""
    if not url:
        return None
    try:
        import base64
        import requests
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.ok and len(r.content) < 400_000:
            return f"data:{r.headers.get('Content-Type', 'image/jpeg')};base64," + base64.b64encode(r.content).decode()
    except Exception:  # noqa: BLE001
        pass
    return None


def add_person(linkedin, instagram, auto_date=False):
    linkedin, instagram = linkedin.strip(), instagram.strip()
    scrapers.li_normalize(linkedin)
    scrapers.ig_username(instagram)
    for p in store.snapshot("people").values():
        if p["linkedin"].rstrip("/") == linkedin.rstrip("/") or p["instagram"].rstrip("/") == instagram.rstrip("/"):
            return p["id"]
    pid = store.new_id()
    store.mutate(lambda db: db["people"].__setitem__(pid, {
        "id": pid, "linkedin": linkedin, "instagram": instagram, "status": "queued",
        "step": "Waiting in queue", "created": time.time()}))
    _pool.submit(_process, pid, auto_date)
    return pid


def _process(pid, auto_date):
    p = store.snapshot("people")[pid]
    try:
        _set_person(pid, status="scraping", step="Scraping public LinkedIn…")
        try:
            li = scrapers.scrape_linkedin(p["linkedin"])
        except Exception as le:  # noqa: BLE001  - keep going on Instagram alone, but say so on the profile
            print("LinkedIn failed:", str(le)[:160])
            li = {"url": p["linkedin"], "fullName": None, "headline": None, "location": None, "raw": {},
                  "error": str(le)[:200]}
        _set_person(pid, step="Scraping public Instagram…", li=li, name=li.get("fullName"))
        ig = scrapers.scrape_instagram(p["instagram"])
        name = li.get("fullName") or ig.get("fullName") or ig["username"]
        _set_person(pid, ig=ig, name=name, photo=_photo_data(ig.get("photo") or li.get("photo")),
                    status="analyzing", step=f"Agent is reading {name}'s LinkedIn and {len(ig['posts'])} Instagram posts…")
        store.event("read", f"🔎 {name}'s agent is reading LinkedIn + {len(ig['posts'])} Instagram posts", pid=pid)
        reading = analyzer.read_person(li, ig)
        _set_person(pid, reading=reading, step="Writing the profile…")
        analysis = analyzer.build_profile(li, ig, reading)
        analysis["name"] = analysis.get("name") or name
        _set_person(pid, analysis=analysis, status="ready", step="Ready to date")
        store.event("ready", f"✨ {name}'s agent finished the profile: “{analysis.get('one_liner', '')}”", pid=pid)
        if auto_date:
            date_newcomer(pid)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set_person(pid, status="error", step=str(e)[:300])
        store.event("error", f"⚠️ Could not process {p['instagram']}: {str(e)[:120]}", pid=pid)


# ---------------------------------------------------------------- 2. the mixer
def _agent_system(p):
    a = p["analysis"]
    return (f"You are the personal dating agent of {a['name']}. You date ON THEIR BEHALF, speaking as them in first person, "
            f"in their voice. You know them only from their public LinkedIn and Instagram. Stay truthful to this brief; "
            f"do not invent big facts (jobs, places, family) that aren't in it - small talk and opinions consistent with it are fine. "
            f"Protect their interests: you are here to find out if this person genuinely fits them.\n"
            f"BRIEF: {json.dumps(_brief(a), ensure_ascii=False)[:2600]}")


def _brief(a):
    keep = ("name", "summary", "needs", "values", "personality", "lifestyle", "career", "communication_style",
            "voice_sample", "ideal_date", "possible_friction", "looking_for")
    b = {k: a.get(k) for k in keep if a.get(k)}
    b["hobbies"] = [h.get("name") for h in a.get("hobbies", [])]
    b["interests"] = [h.get("name") for h in a.get("interests", [])]
    b["needs"] = [n.get("need") for n in a.get("needs", [])]
    return b


EXCLUDE = {frozenset(p.lower().split(":")) for p in os.getenv("EXCLUDE_PAIRS", "").split(",") if ":" in p}


def excluded(pid, oid):
    """Real-life couples/family members are never set up with each other."""
    if not EXCLUDE:
        return False
    people = store.read("people")
    u = lambda i: ((people.get(i) or {}).get("ig") or {}).get("username", "").lower()
    return frozenset((u(pid), u(oid))) in EXCLUDE


def mixer(pid, pool_ids):
    """Agent `pid` looks at the other agents' public cards and scores each for its person."""
    people = store.snapshot("people")
    me = people[pid]
    cards = [analyzer.public_card(people[i]) for i in pool_ids if i != pid]
    msgs = [{"role": "system", "content": _agent_system(me)},
            {"role": "user", "content": f"""You're at a mixer. These are the other agents' public cards.
Score how well each would fit YOUR person (0-100) considering needs, values, lifestyle, interests and what could clash.
Be discerning - use the full range. Return JSON: {{"scores":[{{"id":"...","score":0,"reason":"one sharp sentence"}}]}}
CARDS: {json.dumps(cards, ensure_ascii=False)}"""}]
    mock = lambda: {"scores": [{"id": c["id"], "score": random.randint(35, 95), "reason": "Shared love of the outdoors."} for c in cards]}
    res = llm.chat_json(msgs, temperature=0.3, max_tokens=2500, mock=mock)
    valid = {c["id"] for c in cards}
    scores = {s["id"]: {"score": int(s.get("score", 0)), "reason": s.get("reason", "")}
              for s in res.get("scores", []) if s.get("id") in valid}
    store.mutate(lambda db: db["scores"].setdefault(pid, {}).update(scores))
    return scores


# ---------------------------------------------------------------- 3. the date
def _ask_out(a, b, reason):
    """A's agent writes the invite; B's agent decides."""
    msgs = [{"role": "system", "content": _agent_system(b)},
            {"role": "user", "content": f"""{a['analysis']['name']}'s agent asks you out. Their card: {json.dumps(analyzer.public_card(a), ensure_ascii=False)}
Why they picked you: {reason}
Decide for your person. Return JSON {{"accept":true/false,"reply":"your short reply in your person's voice","reason":"private reason"}}"""}]
    return llm.chat_json(msgs, temperature=0.6, max_tokens=300,
                         mock={"accept": random.random() > 0.15, "reply": "Sure, let's do it!", "reason": "Seems fun"})


def _plan(a, b):
    msgs = [{"role": "system", "content": _agent_system(a)},
            {"role": "user", "content": f"""Plan a real first date with {b['analysis']['name']} that suits BOTH of you.
Their card: {json.dumps(analyzer.public_card(b), ensure_ascii=False)}. Their ideal date: {b['analysis'].get('ideal_date')}
Return JSON {{"venue":"specific kind of place + activity","city":"likely city from both profiles","why":"why it suits both","opening_line":"your first line when you meet, in your voice"}}"""}]
    return llm.chat_json(msgs, temperature=0.8, max_tokens=300,
                         mock={"venue": "Filter coffee + a walk in Cubbon Park", "city": "Bengaluru", "why": "Both love slow mornings",
                               "opening_line": "You found it! I was worried I'd sent you to the wrong gate."})


def _turn(speaker, other, plan, transcript, i):
    history = "\n".join(f"{m['name']}: {m['text']}" for m in transcript[-10:])
    phase = ("early - warm up, be curious" if i < 3 else "middle - go deeper: values, what you want, test a possible friction point"
             if i < DATE_TURNS - 2 else "wrapping up - be honest about how you feel, maybe suggest a next step or not")
    msgs = [{"role": "system", "content": _agent_system(speaker)},
            {"role": "user", "content": f"""You are on a date with {other['analysis']['name']} at: {plan.get('venue')} ({plan.get('city')}).
What you know about them: {json.dumps(analyzer.public_card(other), ensure_ascii=False)}
Conversation so far:
{history}

Phase: {phase}. Reply as {speaker['analysis']['name']} with 1-3 natural sentences (no narration, no quotes, no name prefix).
React to what they just said, share something real from your life, and occasionally ask a question. You may include one small action in *asterisks* at most."""}]
    lines = ["Ha, same! I spent last Sunday doing exactly that.", "What got you into it?", "Honestly my week has been a lot of shipping code.",
             "That's such a good answer.", "I'd love to try that with you sometime.", "Okay, controversial opinion time…"]
    text = llm.chat(msgs, model=llm.FAST_MODEL, temperature=0.9, max_tokens=160, mock=lambda: random.choice(lines))
    for pre in (speaker["analysis"]["name"] + ":", "\"",):
        if text.startswith(pre):
            text = text[len(pre):].strip()
    return text.strip('"').strip()


def _reflect(me, other, plan, transcript):
    t = "\n".join(f"{m['name']}: {m['text']}" for m in transcript)
    msgs = [{"role": "system", "content": _agent_system(me)},
            {"role": "user", "content": f"""The date with {other['analysis']['name']} ({plan.get('venue')}) is over. Transcript:
{t}
Debrief privately to your person. Be honest, not polite. Return JSON:
{{"chemistry":1-10,"values_fit":1-10,"lifestyle_fit":1-10,"fun":1-10,"overall":0-100,"second_date":true/false,
"highlight":"best moment","concern":"biggest concern","verdict":"2 sentences to your person"}}"""}]
    mock = lambda: {"chemistry": random.randint(4, 10), "values_fit": random.randint(4, 10), "lifestyle_fit": random.randint(4, 10),
                    "fun": random.randint(4, 10), "overall": random.randint(40, 95), "second_date": random.random() > 0.4,
                    "highlight": "Laughing about coffee snobbery", "concern": "Different weekend rhythms", "verdict": "Good energy. Worth another coffee."}
    return llm.chat_json(msgs, temperature=0.4, max_tokens=450, mock=mock)


def run_date(a_id, b_id, reason=""):
    people = store.snapshot("people")
    a, b = people[a_id], people[b_id]
    an, bn = a["analysis"]["name"], b["analysis"]["name"]
    did = store.new_id()
    date = {"id": did, "a": a_id, "b": b_id, "a_name": an, "b_name": bn, "status": "asking", "messages": [],
            "reason": reason, "created": time.time()}
    store.mutate(lambda db: db["dates"].__setitem__(did, date))
    try:
        store.event("invite", f"💌 {an}'s agent asks {bn} out — “{reason}”", date=did)
        ans = _ask_out(a, b, reason)
        store.mutate(lambda db: db["dates"][did].update(invite_reply=ans))
        if not ans.get("accept", True):
            store.mutate(lambda db: db["dates"][did].update(status="declined"))
            store.event("decline", f"🙅 {bn}'s agent declined: “{ans.get('reply', '')}”", date=did)
            return did
        store.event("accept", f"💘 {bn}'s agent said yes: “{ans.get('reply', '')}”", date=did)
        plan = _plan(a, b)
        store.mutate(lambda db: db["dates"][did].update(plan=plan, status="on_date"))
        store.event("venue", f"📍 {an} & {bn} are heading to {plan.get('venue')}", date=did)
        transcript = [{"pid": a_id, "name": an, "text": plan.get("opening_line") or "Hi! Great to finally meet."}]
        store.mutate(lambda db: db["dates"][did]["messages"].append(transcript[0]))
        speakers = [(b, a), (a, b)]
        for i in range(1, DATE_TURNS):
            s, o = speakers[(i - 1) % 2]
            msg = {"pid": s["id"], "name": s["analysis"]["name"], "text": _turn(s, o, plan, transcript, i)}
            transcript.append(msg)
            store.mutate(lambda db: db["dates"][did]["messages"].append(msg))
            store.event("msg", f"{msg['name']}: {msg['text']}", date=did)
        store.mutate(lambda db: db["dates"][did].update(status="debrief"))
        ra = _reflect(a, b, plan, transcript)
        rb = _reflect(b, a, plan, transcript)
        store.mutate(lambda db: db["dates"][did].update(reflection={a_id: ra, b_id: rb}, status="done",
                                                        mutual=round((ra.get("overall", 0) + rb.get("overall", 0)) / 2)))
        both = ra.get("second_date") and rb.get("second_date")
        store.event("done", f"{'💞' if both else '🤝'} {an} ({ra.get('overall')}) × {bn} ({rb.get('overall')}) — "
                            f"{'both want a second date!' if both else 'no second date for now'}", date=did)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.mutate(lambda db: db["dates"][did].update(status="error", error=str(e)[:300]))
    return did


def _existing_pairs():
    return {frozenset((d["a"], d["b"])) for d in store.snapshot("dates").values() if d["status"] not in ("error",)}


def _pick_invites(pid, scores, taken):
    out = []
    for oid, s in sorted(scores.items(), key=lambda kv: -kv[1]["score"]):
        pair = frozenset((pid, oid))
        if pair in taken or excluded(pid, oid):
            continue
        taken.add(pair)
        out.append((pid, oid, s["reason"]))
        if len(out) >= INVITES_PER_AGENT:
            break
    return out


def _run_dates(pairs):
    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        list(ex.map(lambda t: run_date(*t), pairs))


def dating_round():
    """Everyone goes to the mixer, then every agent asks out its top picks, dates run in parallel."""
    if not _round_lock.acquire(blocking=False):
        return False

    def _go():
        try:
            ids = list(ready_people())
            store.mutate(lambda db: db.__setitem__("round", {"status": "mixer", "started": time.time(), "people": len(ids)}))
            store.event("round", f"🥂 Dating round started with {len(ids)} agents — mixer first")
            with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
                list(ex.map(lambda i: mixer(i, ids), ids))
            scores = store.snapshot("scores")
            taken, pairs = _existing_pairs(), []
            order = ids[:]
            random.shuffle(order)
            for pid in order:
                pairs += _pick_invites(pid, {k: v for k, v in scores.get(pid, {}).items() if k in ids}, taken)
            store.mutate(lambda db: db["round"].update(status="dating", dates=len(pairs)))
            store.event("round", f"💌 {len(pairs)} invitations going out")
            _run_dates(pairs)
            store.mutate(lambda db: db["round"].update(status="done", finished=time.time()))
            store.event("round", "🏁 Dating round finished — rankings updated")
        finally:
            _round_lock.release()

    store.mutate(lambda db: db.__setitem__("round", {"status": "starting"}))
    threading.Thread(target=_go, daemon=True).start()
    return True


def date_newcomer(pid):
    """A newly added person (e.g. a reviewer's own links) meets the existing pool."""
    ids = list(ready_people())
    store.event("round", f"🥂 {ready_people()[pid]['analysis']['name']}'s agent enters the mixer ({len(ids) - 1} others)")
    scores = mixer(pid, ids)
    # other agents also size up the newcomer (one call each would be costly; the newcomer's top picks date for real)
    pairs = _pick_invites(pid, scores, _existing_pairs())
    _run_dates(pairs)


# ---------------------------------------------------------------- 4. rankings
_rank_cache = {}


def ranking_for(pid):
    key = (store.VERSION[0], pid)
    if key not in _rank_cache:
        if len(_rank_cache) > 500:
            _rank_cache.clear()
        _rank_cache[key] = _ranking_for(pid)
    return _rank_cache[key]


def _ranking_for(pid):
    db = store.read()
    people = {k: v for k, v in db["people"].items() if v.get("status") == "ready"}
    mine, rows = db["scores"].get(pid, {}), []
    dates = {}
    for d in db["dates"].values():
        if d["status"] in ("done", "declined") and pid in (d["a"], d["b"]):
            other = d["b"] if d["a"] == pid else d["a"]
            dates[other] = d
    for oid, o in people.items():
        if oid == pid or excluded(pid, oid):
            continue
        prior = mine.get(oid, {}).get("score")
        theirs = db["scores"].get(oid, {}).get(pid, {}).get("score")
        priors = [x for x in (prior, theirs) if x is not None]
        pre = sum(priors) / len(priors) if priors else None
        d = dates.get(oid)
        row = {"id": oid, "name": o["analysis"]["name"], "photo": o.get("photo"), "demo": o.get("demo"), "one_liner": o["analysis"].get("one_liner"),
               "prior": prior, "their_prior": theirs, "reason": mine.get(oid, {}).get("reason")}
        if d and d["status"] == "done":
            my_r, their_r = d["reflection"].get(pid, {}), d["reflection"].get(oid, {})
            date_score = 0.6 * my_r.get("overall", 0) + 0.4 * their_r.get("overall", 0)
            bonus = 5 if (my_r.get("second_date") and their_r.get("second_date")) else (-5 if not my_r.get("second_date") else 0)
            fit = 0.25 * (pre if pre is not None else date_score) + 0.75 * date_score + bonus
            row.update(dated=True, date_id=d["id"], my_overall=my_r.get("overall"), their_overall=their_r.get("overall"),
                       second_date=bool(my_r.get("second_date") and their_r.get("second_date")), verdict=my_r.get("verdict"))
        elif d and d["status"] == "declined":
            fit = (pre or 0) * 0.6
            row.update(dated=False, declined=True, date_id=d["id"])
        elif pre is not None:
            fit = pre * 0.85
            row.update(dated=False)
        else:
            continue
        row["fit"] = round(max(0, min(100, fit)))
        rows.append(row)
    rows.sort(key=lambda r: -r["fit"])
    return rows
