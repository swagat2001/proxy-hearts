"""The agent reads its person (LinkedIn + Instagram only) in two passes: a cited reading log, then a profile."""
import json

import llm

GUARD = ("Use ONLY the LinkedIn and Instagram data given. Every claim must be traceable to it. "
         "Never infer or mention religion, caste, ethnicity, sexual orientation, health, politics, income, or appearance/attractiveness. "
         "If the data is thin, say so and lower confidence instead of inventing.")


def _source_pack(li, ig):
    posts = [{k: p.get(k) for k in ("date", "caption", "hashtags", "location", "alt", "likes")} for p in ig.get("posts", [])]
    return json.dumps({
        "LINKEDIN": {k: li.get(k) for k in ("fullName", "headline", "location")} | {"record": li.get("raw")},
        "INSTAGRAM": {k: ig.get(k) for k in ("username", "fullName", "biography", "category", "externalUrl", "followers", "following", "postsCount")}
        | {"posts": posts},
    }, ensure_ascii=False)[:9000]


def read_person(li, ig):
    """Pass 1: the reading. Observations, each with a source, like an analyst's notes."""
    name = li.get("fullName") or ig.get("fullName") or ig.get("username")
    msgs = [
        {"role": "system", "content": "You are a perceptive dating agent about to represent a real person. " + GUARD},
        {"role": "user", "content": f"""Read {name}'s two sources carefully, LinkedIn first, then every Instagram post.
Write your reading log: 10-14 concrete observations. For each, quote or point to the evidence and say what it suggests about them as a partner.
Look for patterns across posts (recurring places, activities, people, time of day, tone of captions, what they're proud of, what they post about work vs life).

Return JSON: {{"observations":[{{"source":"LinkedIn|Instagram","evidence":"short quote or description","insight":"what it suggests"}}],
"patterns":["cross-source patterns"], "gaps":["what the data does not tell us"]}}

DATA:
{_source_pack(li, ig)}"""}]
    mock = {"observations": [{"source": "Instagram", "evidence": p["caption"], "insight": "Enjoys this regularly"} for p in ig.get("posts", [])[:4]]
            + [{"source": "LinkedIn", "evidence": li.get("headline", ""), "insight": "Career-driven builder"}],
            "patterns": ["Weekends are for hobbies"], "gaps": ["Nothing about family"]}
    return llm.chat_json(msgs, temperature=0.4, max_tokens=2200, mock=mock)


def build_profile(li, ig, reading):
    """Pass 2: synthesize the profile page the agent will date with."""
    name = li.get("fullName") or ig.get("fullName") or ig.get("username")
    msgs = [
        {"role": "system", "content": "You are a dating agent writing the private brief you will use to date on your person's behalf. " + GUARD},
        {"role": "user", "content": f"""Person: {name}
Your reading log: {json.dumps(reading, ensure_ascii=False)[:6000]}
Headline: {li.get('headline')} | IG bio: {ig.get('biography')}

Return JSON with exactly these keys:
{{"name":"{name}","one_liner":"a warm 1-sentence dating tagline",
"summary":"3-4 sentences - who they are as a person and partner",
"needs":[{{"need":"what they need in a partner/relationship","why":"evidence-based reason"}}] (4-6 items),
"hobbies":[{{"name":"...","evidence":"..."}}], "interests":[{{"name":"...","evidence":"..."}}],
"values":["..."], "personality":{{"traits":["..."],"social_energy":"introvert/ambivert/extrovert + why","humor":"..."}},
"lifestyle":"rhythm of their life, travel, fitness, city", "career":"work & ambition in 1-2 sentences",
"communication_style":"how they would text/talk", "voice_sample":"one message they might send on a date, in their voice",
"ideal_date":"specific ideal first date", "green_flags":["..."], "possible_friction":["what could clash in a relationship"],
"conversation_hooks":["topics that would light them up"], "looking_for":"the kind of person that would fit them",
"confidence":0-100}}"""}]
    mock = {"name": name, "one_liner": f"{name} - builder by week, explorer by weekend", "summary": "Curious, driven and warm.",
            "needs": [{"need": "A partner with their own passions", "why": "Posts show independent hobbies"}],
            "hobbies": [{"name": o["evidence"][:30], "evidence": "IG post"} for o in reading.get("observations", [])[:3]],
            "interests": [{"name": "startups", "evidence": "LinkedIn"}], "values": ["growth", "kindness"],
            "personality": {"traits": ["curious", "playful"], "social_energy": "ambivert", "humor": "dry"},
            "lifestyle": "Active city life", "career": li.get("headline"), "communication_style": "short, witty",
            "voice_sample": "Okay but have you tried the filter coffee at that place?", "ideal_date": "Coffee walk then a bookstore",
            "green_flags": ["consistent"], "possible_friction": ["busy schedule"], "conversation_hooks": ["side projects"],
            "looking_for": "someone adventurous", "confidence": 70}
    return llm.chat_json(msgs, temperature=0.5, max_tokens=2200, mock=mock)


def public_card(p):
    """What other agents are allowed to see before a date (like a dating-app card)."""
    a = p.get("analysis") or {}
    return {"id": p["id"], "name": a.get("name") or p.get("name"), "one_liner": a.get("one_liner"),
            "hobbies": [h.get("name") for h in a.get("hobbies", [])][:6],
            "interests": [h.get("name") for h in a.get("interests", [])][:6],
            "values": a.get("values", [])[:5], "looking_for": a.get("looking_for"),
            "traits": (a.get("personality") or {}).get("traits", [])[:5], "lifestyle": a.get("lifestyle"),
            "career": a.get("career")}
