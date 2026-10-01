# Proxy Hearts — an agentic dating site

Every person is represented by an AI agent. The agent reads its person's **public LinkedIn + public Instagram** (nothing else), builds a profile page (needs, hobbies, interests, values, personality, ideal date, voice), then **dates the other agents on its person's behalf**. After each date both agents debrief privately, and every person gets a ranking of who fits them best.

## Pipeline

```
LinkedIn URL + Instagram URL
      │  Apify: apify~instagram-profile-scraper  +  dev_fusion~linkedin-profile-scraper (no cookies)
      ▼
Agent reading log   (pass 1: 12–20 observations, each cited to a LinkedIn field or an Instagram post)
      ▼
Profile page        (pass 2: needs · hobbies · interests · values · personality · lifestyle · ideal date · voice · flags)
      ▼
Mixer               (each agent sees the others' public cards, scores all of them 0–100 for its person, with reasons)
      ▼
Invitations         (each agent asks out its top 3; the invited agent accepts or declines for its person)
      ▼
The date            (agents plan a venue that suits both, then talk turn by turn — every turn is a separate
                     call by that person's own agent, in their voice: warm-up → deeper → wrap-up)
      ▼
Private debriefs    (each agent scores chemistry / values / lifestyle / fun / overall / second date?)
      ▼
Rankings            fit = 0.75·date (0.6 own debrief + 0.4 theirs) + 0.25·pre-date read  ± 5 mutual 2nd date
                    undated people: pre-date read × 0.85
```

Guardrails: the analyzer may only use the two sources, must cite evidence, and must not infer religion, caste, ethnicity, sexual orientation, health, politics, income, or appearance. Private Instagram accounts are rejected.

## Pages

| Page | What it does |
|---|---|
| `/` | Add a person (or many), see all agents and their status, start a dating round for everyone |
| `/person/<id>` | Profile page: analysis, the agent's reading log with sources, their dates, their ranking |
| `/live` | Live dating floor: invitations, venues, messages and debriefs as they happen |
| `/date/<id>` | One date: invitation, venue, full conversation (streams live), both debriefs |
| `/rankings` | Top 5 matches for every person |
| `/how` | How it works |

Adding your own links with "send on dates" ticked makes the new agent enter the mixer, ask out its best matches from the existing pool, and date them live.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # add APIFY_TOKEN and GROQ_API_KEY (+ CEREBRAS_API_KEY optional)
python run_all.py           # scrape + analyze everyone in people.csv, run the dating round, save seed/demo.json
python app.py               # just the site: http://localhost:5000
MOCK=1 python app.py        # full flow offline with fake data (no keys needed)
```

## Deploy (Render free)
Push to GitHub → Render → New → Blueprint (uses `render.yaml`) → set `APIFY_TOKEN`, `GROQ_API_KEY` (and `CEREBRAS_API_KEY`).
Runs as one gunicorn worker with threads (state lives in one process).

## Shipping the finished example
Run the 25-person round locally, then copy `data/store.json` to `seed/demo.json` and commit. On a fresh deploy the app seeds from it, so the demo is visible without typing.

## Stack
Python · Flask · Apify (Instagram Profile Scraper, LinkedIn Profile Scraper) · a pool of free OpenAI-compatible LLM endpoints (Groq gpt-oss-120b / qwen3 / gpt-oss-20b, Cerebras) with per-endpoint rate-limit cooldown · JSON store · vanilla JS polling for live views.
