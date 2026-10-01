"""Scrape the two allowed sources: a public LinkedIn profile and a public Instagram profile, via Apify actors."""
import json
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import re

import requests

MOCK = os.getenv("MOCK", "0") == "1" or os.getenv("MOCK_SCRAPE") == "1"
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
IG_ACTOR = os.getenv("IG_ACTOR", "apify~instagram-profile-scraper")
# Any LinkedIn profile actor from the Apify Store works; set its id and the name of its URL-list input field.
# Tried in order until one works: "actor_id:input_field_for_url_list"
LI_ACTORS = [a.split(":") for a in os.getenv(
    "LI_ACTORS", "harvestapi~linkedin-profile-scraper:urls,dev_fusion~linkedin-profile-scraper:profileUrls").split(",") if ":" in a]
IG_POSTS = int(os.getenv("IG_POSTS", "12"))


class ScrapeError(Exception):
    pass


def ig_username(url):
    url = url.strip()
    if "instagram.com" not in url:
        return url.lstrip("@").strip("/")
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", url)
    if not m or m.group(1) in ("p", "reel", "stories", "explore"):
        raise ScrapeError("Not an Instagram profile link")
    return m.group(1)


def li_normalize(url):
    url = url.strip().split("?")[0].rstrip("/")
    if "linkedin.com/in/" not in url:
        raise ScrapeError("Not a LinkedIn /in/ profile link")
    return "https://www.linkedin.com/in/" + url.split("linkedin.com/in/")[1]


def run_actor(actor, payload, timeout=300):
    if not APIFY_TOKEN:
        raise ScrapeError("APIFY_TOKEN not set on the server")
    r = requests.post(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                      params={"token": APIFY_TOKEN}, json=payload, timeout=timeout)
    if r.status_code >= 400:
        raise ScrapeError(f"Apify {actor} failed: {r.status_code} {r.text[:200]}")
    return r.json()


def run_actor_async(actor, payload, max_wait=900):
    """Start a run, poll until it finishes, return dataset items (for big batches that exceed the sync timeout)."""
    import time
    if not APIFY_TOKEN:
        raise ScrapeError("APIFY_TOKEN not set")
    r = requests.post(f"https://api.apify.com/v2/acts/{actor}/runs", params={"token": APIFY_TOKEN}, json=payload, timeout=60)
    if r.status_code >= 400:
        raise ScrapeError(f"Apify {actor} failed to start: {r.status_code} {r.text[:200]}")
    run = r.json()["data"]
    t0 = time.time()
    while run["status"] in ("READY", "RUNNING") and time.time() - t0 < max_wait:
        time.sleep(8)
        run = requests.get(f"https://api.apify.com/v2/actor-runs/{run['id']}", params={"token": APIFY_TOKEN}, timeout=60).json()["data"]
        print(f"  [{actor}] {run['status']} {int(time.time() - t0)}s")
    items = requests.get(f"https://api.apify.com/v2/datasets/{run['defaultDatasetId']}/items",
                         params={"token": APIFY_TOKEN, "clean": "true"}, timeout=120).json()
    return items


_IG_CACHE, _LI_CACHE = {}, {}


def prefetch(pairs):
    """Batch-scrape many people in two Apify runs (much faster and cheaper than one run per person)."""
    users = [ig_username(ig) for _, ig in pairs]
    urls = [li_normalize(li) for li, _ in pairs]
    if MOCK:
        return
    import threading
    out = {}

    def ig():
        try:
            out["ig"] = run_actor_async(IG_ACTOR, {"usernames": users, "resultsLimit": IG_POSTS})
        except Exception as e:  # noqa: BLE001
            print("IG batch failed:", e)

    def li():
        for actor, key in LI_ACTORS:
            try:
                items = run_actor_async(actor, {key: urls})
                if items:
                    out["li"] = items
                    print(f"LinkedIn batch OK via {actor}: {len(items)} profiles")
                    return
            except Exception as e:  # noqa: BLE001
                print(f"LinkedIn batch via {actor} failed: {str(e)[:160]}")
    try:
        _prev = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "apify_cache.json"), encoding="utf-8"))
        have_ig = {(p.get("username") or "").lower() for p in _prev.get("ig", [])}
        have_li = len(_prev.get("li", [])) >= len(urls)
    except Exception:  # noqa: BLE001
        have_ig, have_li = set(), False
    ts = []
    if not all(u.lower() in have_ig for u in users):
        ts.append(threading.Thread(target=ig))
    else:
        print("Instagram: using cached scrape")
    if not have_li:
        ts.append(threading.Thread(target=li))
    [t.start() for t in ts]
    [t.join() for t in ts]
    import json as _j
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "apify_cache.json")
    try:
        prev = _j.load(open(cache, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        prev = {"ig": [], "li": []}
    for k in ("ig", "li"):
        out[k] = (out.get(k) or []) + prev.get(k, [])
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    _j.dump(out, open(cache, "w", encoding="utf-8"))
    for p in out.get("ig") or []:
        if p.get("username"):
            _IG_CACHE[p["username"].lower()] = p
    for p in out.get("li") or []:
        key = (p.get("linkedinUrl") or p.get("url") or p.get("profileUrl") or p.get("inputUrl") or "").split("?")[0].rstrip("/").lower()
        pid = p.get("publicIdentifier") or p.get("public_identifier") or key.rsplit("/", 1)[-1]
        if pid:
            _LI_CACHE[pid.lower()] = p
    print(f"prefetched {len(_IG_CACHE)} instagram, {len(_LI_CACHE)} linkedin")


def scrape_instagram(url):
    user = ig_username(url)
    if MOCK:
        return _mock_ig(user)
    p = _IG_CACHE.get(user.lower())
    if not p:
        items = run_actor(IG_ACTOR, {"usernames": [user], "resultsLimit": IG_POSTS})
        if not items or items[0].get("error"):
            raise ScrapeError(f"Instagram profile @{user} not found")
        p = items[0]
    if p.get("private") or p.get("isPrivate"):
        raise ScrapeError(f"@{user} is private - only public Instagram profiles are allowed")
    posts = []
    for x in (p.get("latestPosts") or [])[:IG_POSTS]:
        posts.append({
            "caption": (x.get("caption") or "")[:450],
            "hashtags": x.get("hashtags") or [],
            "location": x.get("locationName"),
            "type": x.get("type"),
            "alt": (x.get("alt") or "")[:300],   # Instagram's own image description
            "likes": x.get("likesCount"),
            "date": (x.get("timestamp") or "")[:10],
            "image": x.get("displayUrl"),
            "url": x.get("url"),
        })
    return {
        "username": p.get("username") or user,
        "fullName": p.get("fullName"),
        "biography": p.get("biography"),
        "externalUrl": p.get("externalUrl"),
        "category": p.get("businessCategoryName"),
        "followers": p.get("followersCount"),
        "following": p.get("followsCount"),
        "postsCount": p.get("postsCount"),
        "verified": p.get("verified"),
        "photo": p.get("profilePicUrlHD") or p.get("profilePicUrl"),
        "posts": posts,
        "url": f"https://www.instagram.com/{user}/",
    }


_DROP = re.compile(r"(urn|tracking|entityUrn|logo|backgroundPic|companyId|^id$)", re.I)


def _slim(o, depth=0):
    """Strip noise so the whole LinkedIn record fits in the agent's context, whatever actor schema is used."""
    if depth > 6:
        return None
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if _DROP.search(k) or v in (None, "", [], {}):
                continue
            if isinstance(v, str) and v.startswith("http") and "pic" not in k.lower() and "photo" not in k.lower():
                continue
            sv = _slim(v, depth + 1)
            if sv not in (None, "", [], {}):
                out[k] = sv
        return out
    if isinstance(o, list):
        return [x for x in (_slim(i, depth + 1) for i in o[:25]) if x not in (None, "", [], {})]
    if isinstance(o, str):
        return o[:800]
    return o


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v if isinstance(v, str) else (v.get("url") if isinstance(v, dict) else None)
    return None


def scrape_linkedin(url):
    url = li_normalize(url)
    if MOCK:
        return _mock_li(url)
    raw = _LI_CACHE.get(url.rsplit("/", 1)[-1].lower())
    if not raw:
        errs = []
        for actor, key in LI_ACTORS:
            try:
                items = run_actor(actor, {key: [url]})
                if items:
                    raw = items[0]
                    break
                errs.append(f"{actor}: empty")
            except Exception as e:  # noqa: BLE001
                errs.append(str(e)[:120])
        if not raw:
            raise ScrapeError("LinkedIn scrape failed: " + " | ".join(errs))
    name = _pick(raw, "fullName", "full_name", "name") or " ".join(
        x for x in [raw.get("firstName"), raw.get("lastName")] if x)
    return {
        "url": url,
        "fullName": name,
        "headline": _pick(raw, "headline", "occupation", "jobTitle"),
        "location": _pick(raw, "addressWithCountry", "location", "geoLocationName", "city"),
        "photo": _pick(raw, "profilePicHighQuality", "profilePic", "profilePicture", "photoUrl", "profile_pic_url"),
        "raw": _slim(raw),
    }


# ---------------- mock data (MOCK=1) for offline testing ----------------
_H = ["trail running", "specialty coffee", "chess", "street photography", "baking sourdough", "salsa dancing",
      "indie films", "bouldering", "sketching", "cycling", "reading sci-fi", "cooking biryani", "yoga", "football"]


def _mock_ig(user):
    h = abs(hash(user))
    hobbies = [_H[(h >> i) % len(_H)] for i in (0, 4, 8)]
    return {"username": user, "fullName": user.replace("_", " ").title(), "biography": f"{hobbies[0]} | {hobbies[1]} | building things",
            "followers": h % 5000, "following": 300, "postsCount": 40, "photo": None, "url": f"https://www.instagram.com/{user}/",
            "posts": [{"caption": f"Sunday = {x} ☀️ #{x.replace(' ', '')}", "hashtags": [x.replace(" ", "")], "location": "Bengaluru",
                       "alt": f"May be an image of 1 person, {x}", "date": "2026-08-01", "likes": 120} for x in hobbies * 2]}


def _mock_li(url):
    slug = url.rsplit("/", 1)[-1]
    return {"url": url, "fullName": slug.replace("-", " ").title(), "headline": "Product Engineer at a startup",
            "location": "Bengaluru, India", "photo": None,
            "raw": {"about": "I like building products people love.", "experiences": [{"title": "Engineer", "company": "Startup"}],
                    "skills": ["Python", "Design"], "volunteer": ["Teaching kids to code"]}}


if __name__ == "__main__":
    import sys
    print(json.dumps(scrape_instagram(sys.argv[1]) if "instagram" in sys.argv[1] else scrape_linkedin(sys.argv[1]), indent=1)[:4000])
