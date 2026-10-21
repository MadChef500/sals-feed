#!/usr/bin/env python3
"""
Sal's Feed - a free personal RSS aggregator. (v2, 2026-10-20)

Fetches ~28 reputable RSS feeds across five lanes (major breaking news,
Philadelphia local, Philly sports, Tesla, AI), scores each story for genuine
significance, deduplicates across outlets, ranks by importance x recency,
and emits a tight RSS 2.0 feed of the stories actually worth reading.

v2 lessons from v1's failures (Sal, 2026-10-20: "the feeds suck"):
- Freshness is judged by PUBLICATION date, never by when we first saw a
  story. A source re-listing an 8-day-old story must not resurrect it.
- Scoring reads the HEADLINE only. Summaries inflated opinion pieces.
- A story can only compete in lanes its sources belong to (plus breaking
  for general/local sources). A Flyers rumor can never become "breaking".
- Every lane has a hard news-event gate: no event keyword in the headline,
  no slot - no matter how many outlets carry the piece.
- Opinion / column / feature / trade-pitch / troll phrasing is vetoed.
- Tesla items must be relevant to a US owner (no China-built-only recalls,
  no "petition asking for a probe" non-news).

Runs on GitHub Actions (free) every 20 minutes; GitHub Pages serves feed.xml.
State (seen stories, HTTP caches) persists in state.json in the repo.
"""
import json
import os
import re
import sys
import html as htmlmod
from datetime import datetime, timezone
from email.utils import format_datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

import feedparser
import requests

# ---------------------------------------------------------------- config

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
FEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.xml")

UA = {"User-Agent": "SalsFeedBot/2.0 (personal RSS aggregator, polling every 20 min)"}
TIMEOUT = 25

SOURCES = [
    # ---- major breaking / world / national ----
    {"cat": "breaking", "name": "BBC News", "url": "https://feeds.bbci.co.uk/news/rss.xml", "trust": 3},
    {"cat": "breaking", "name": "NPR News", "url": "https://feeds.npr.org/1001/rss.xml", "trust": 3},
    {"cat": "breaking", "name": "PBS NewsHour", "url": "https://www.pbs.org/newshour/feeds/rss/headlines", "trust": 3},
    {"cat": "breaking", "name": "Google News Top Stories", "url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en", "trust": 2, "strip_suffix": True},
    {"cat": "breaking", "name": "Google News Breaking", "url": "https://news.google.com/rss/search?q=breaking%20news&hl=en-US&gl=US&ceid=US:en", "trust": 2, "strip_suffix": True},
    {"cat": "breaking", "name": "CBS News", "url": "https://www.cbsnews.com/latest/rss/main", "trust": 2},
    {"cat": "breaking", "name": "ABC News", "url": "https://abcnews.go.com/abcnews/topstories", "trust": 2},
    {"cat": "breaking", "name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "trust": 2},
    # ---- philadelphia / delco local ----
    {"cat": "local", "name": "Google News Philadelphia", "url": "https://news.google.com/rss/search?q=Philadelphia&hl=en-US&gl=US&ceid=US:en", "trust": 2, "strip_suffix": True},
    {"cat": "local", "name": "6abc Action News", "url": "https://6abc.com/feed/", "trust": 3},
    {"cat": "local", "name": "WHYY", "url": "https://whyy.org/feed/", "trust": 3},
    {"cat": "local", "name": "PhillyVoice", "url": "https://www.phillyvoice.com/feed/", "trust": 2},
    # ---- philly sports ----
    {"cat": "sports", "name": "Bleeding Green Nation", "url": "https://www.bleedinggreennation.com/rss/index.xml", "trust": 3, "team": "eagles"},
    {"cat": "sports", "name": "Broad Street Hockey", "url": "https://www.broadstreethockey.com/rss/index.xml", "trust": 3, "team": "flyers"},
    {"cat": "sports", "name": "The Good Phight", "url": "https://www.thegoodphight.com/rss/index.xml", "trust": 3, "team": "phillies"},
    {"cat": "sports", "name": "Liberty Ballers", "url": "https://www.libertyballers.com/rss/index.xml", "trust": 3, "team": "sixers"},
    {"cat": "sports", "name": "Crossing Broad", "url": "https://www.crossingbroad.com/feed/", "trust": 2},
    {"cat": "sports", "name": "Google News Philly Sports", "url": "https://news.google.com/rss/search?q=Philadelphia%20Eagles%20OR%20Flyers%20OR%20Phillies%20OR%2076ers&hl=en-US&gl=US&ceid=US:en", "trust": 2, "strip_suffix": True},
    # ---- tesla (owner-relevant) ----
    {"cat": "tesla", "name": "Teslarati", "url": "https://www.teslarati.com/feed/", "trust": 3},
    {"cat": "tesla", "name": "Not a Tesla App", "url": "https://www.notateslaapp.com/rss", "trust": 3},
    {"cat": "tesla", "name": "Electrek", "url": "https://electrek.co/feed/", "trust": 2, "tesla_only": True},
    {"cat": "tesla", "name": "InsideEVs", "url": "https://insideevs.com/feed/", "trust": 2, "tesla_only": True},
    {"cat": "tesla", "name": "Google News Tesla", "url": "https://news.google.com/rss/search?q=Tesla&hl=en-US&gl=US&ceid=US:en", "trust": 2, "strip_suffix": True},
    # ---- ai (new models, skills, real-world use) ----
    {"cat": "ai", "name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "trust": 3},
    {"cat": "ai", "name": "Google News AI", "url": "https://news.google.com/rss/search?q=artificial%20intelligence%20OR%20ChatGPT%20OR%20Claude&hl=en-US&gl=US&ceid=US:en", "trust": 2, "strip_suffix": True},
]

# (phrase, points) tiers, matched against the HEADLINE only.
# The first tier of each lane is its news-event tier: the gate below
# requires a hit there (breaking also accepts its second tier or a
# real casualty count in the headline).
KEYWORDS = {
    "breaking": [
        (["terrorist attack", "terror attack", "assassination", "assassinated",
          "airliner", "passenger jet",
          "rocket explod", "nuclear ", "invasion", "invades", "declare war",
          "declares war", "missile strike", "military coup", "martial law",
          "state of emergency"], 120),
        (["mass shooting", "school shooting", "plane crash",
          "earthquake", "hurricane", "tornado emergency", "tsunami",
          "volcano eruption", "major wildfire", "explosion", "building collapse",
          "bridge collapse", "train derail", "hostage", "evacuation order",
          "evacuations ordered", "supreme court", "strikes down",
          "plane down"], 60),
        (["breaking", "emergency", "killed", "dead", "deadly", "deadly shooting",
          "resigns", "dies at",
          "passed away", "indicted", "crash", "crashed", "erupts"], 15),
    ],
    "local": [
        (["tornado warning", "tornado emergency", "flash flood",
          "severe thunderstorm warning", "septa", "phl airport",
          "amtrak", "i-95", "schuylkill", "evacuation", "state of emergency",
          "without power", "power outage", "water main", "bridge collapse",
          "bridge closed", "shut down", "shutdown", "closure"], 80),
        (["mayor", "airport", "crash", "pileup", "strike", "school district",
          "city council", "infrastructure", "transit", "delaware county",
          "shooting", "stabbing", "fire"], 35),
    ],
    "sports": [
        (["out for season", "season-ending", "torn acl", "achilles", "injured reserve",
          "trade", "traded", "acquires", "acquired", "signs ", "signing", "signed",
          "contract extension", "agree to deal", "agrees to deal", "fired", "firing",
          "part ways", "playoff", "clinch", "eliminated", "suspended", "suspension",
          "arrest", "retire", "retirement", "blood clot",
          "winning streak", "losing streak", "win streak", "no-hitter",
          "franchise record", "career-high", "career high"], 80),
        (["injury", "injuries", "injured", "questionable", "doubtful", "will miss",
          "ruled out", "roster move", "waived", "released", "depth chart"], 30),
    ],
    "tesla": [
        (["recall", "software update", "fsd", "full self-driving", "robotaxi",
          "supercharger", "new model", "price cut", "price increase",
          "raises prices", "lowers prices", "cuts prices", "release notes",
          "hardware 5", "ai5", "nhtsa investigation", "nhtsa opens",
          "nhtsa launches", "probe opened", "investigation opened"], 80),
        (["update", "version 20", "model y", "model 3", "cybertruck", "semi",
          "roadster", "range", "charging"], 30),
    ],
    "ai": [
        (["new model", "model release", "model update", "gpt-",
          "claude ", "outage", "data breach", "shuts down",
          "new feature", "new skill", "small business"], 80),
        (["chatgpt", "gemini", "copilot", "launches", "release", "unveil",
          "ai agent", "agents", "skills", "at work", "workplace"], 30),
    ],
}

# Hard vetoes, checked on the headline before any scoring.
GLOBAL_VETO = ["opinion", "editorial", "op-ed", "trade pitch", "trade proposal",
               "mock draft", "power rankings", "mailbag", "podcast",
               "winners and losers", "bold predictions", "overreactions",
               "sponsored", "gift guide", "horoscope", "crossword",
               "troll", "trolls", "trolled", "promo code", "how to watch",
               "live chat", "game thread", "gamethread", "fantasy football",
               "best bets", "betting odds", "prevented", "averted", "foiled",
               "plot to", "heartbreak", "quiz", "memoir",
               "explained", "explainer"]
SPORTS_VETO = ["breakout is coming", "perfect time", "getting harder to see",
               "ends well", "stock up", "stock down", "takeaways",
               "what we learned", "grades", "film review", "film study",
               "preview", "prediction", "predictions", "predicted", "predicts",
               "rumor", "rumors",
               "rumored", "speculation", "could trade", "should trade",
               "landing spots", "best fits", "believes", "fire sale",
               "urged to", "smell blood", "why", "simulator",
               "playoff picture", "exploring trade", "trade market for",
               "could replace", "not being punished", "no punishment",
               "won't punish", "will not punish", "not punish",
               "no discipline", "appropriately", "clears of wrongdoing",
               "cleared of wrongdoing", "no charges", "will not be punished"]
TESLA_VETO = ["stock", "tsla", "shares ", "shareholder", "analyst",
              "price target", "earnings", "grok", "spacex", "neuralink",
              "sponsored", "rumor", "render", "concept", "musk's pay",
              "compensation package", "china-built", "china built",
              "made in china", "shanghai", "giga shanghai", "berlin-built",
              "germany-built", "petition", "weighs probe", "considers probe",
              "urged to probe", "asked to probe", "could probe", "may probe"]
AI_VETO = ["stock", "shares ", "shareholder", "price target", "earnings",
           "sponsored", "gift guide", "raises", "funding", "seed round",
           "series a", "valuation", "rumor", "leak", "bets on", "bet on",
           "alternatives", "used claude to", "used chatgpt to",
           "free tools", "i built", "i used chatgpt", "i used claude"]
BREAKING_VETO = ["one family's", "essay", "what it was like", "test identifies",
                 "recipe", "kardashian", "taylor swift", "bachelor",
                 "real housewives"]
LOCAL_VETO = ["best restaurants", "now open", "for sale", "housing market",
              "home prices", "gift guide", "sponsored", "spotted", "gossip"]
ROUTINE_LOCAL = ["house fire", "vehicle fire", "trash fire", "brush fire",
                 "garage fire", "kitchen fire"]
# Small general-aviation crashes are routine news, not national breaking
# news, unless the headline itself reports a major death toll.
SMALL_AIRCRAFT = ["small plane", "single-engine", "single engine",
                  "private plane", "small aircraft", "prop plane", "cessna"]

LANE_VETO = {"sports": SPORTS_VETO, "tesla": TESLA_VETO, "ai": AI_VETO,
             "breaking": BREAKING_VETO, "local": LOCAL_VETO}

HALF_LIFE = {"breaking": 10, "local": 16, "sports": 18, "tesla": 30, "ai": 30}
THRESHOLD = {"breaking": 60, "local": 45, "sports": 45, "tesla": 45, "ai": 45}
CAT_CAP = {"breaking": 3, "local": 2, "sports": 3, "tesla": 2, "ai": 1}
MAX_ITEMS = 8
# Hard age limits by PUBLICATION date, per lane. Older = never shown.
MAX_PUB_AGE = {"breaking": 36, "local": 48, "sports": 48, "tesla": 72, "ai": 72}

EMOJI = {"breaking": "\U0001F30D", "local": "\U0001F3D9\uFE0F",
         "sports": "\U0001F3DF\uFE0F", "tesla": "\u26A1",
         "ai": "\U0001F916"}
TEAM_EMOJI = {"eagles": "\U0001F985", "flyers": "\U0001F3D2",
              "phillies": "\u26BE", "sixers": "\U0001F3C0"}

STOPWORDS = set(("the a an and or of to in on for with at by from as is are was were "
                 "be been has have had it its this that these those they them he she we you "
                 "his her their our your vs v s new after over under amid among during "
                 "will would could should says said say about into out up off no not but if "
                 "than then so what when where who how why all any each few more most other "
                 "some such only own same too very can just don now live update updates "
                 "watch sources source multiple several people person persons police officials "
                 "official including report reports reported".split()))

PHILLY_ANCHOR = re.compile(
    r"philadelphia|philly|delaware county|\bdelco\b|septa|schuylkill|center city|"
    r"south jersey|pennsylvania|new jersey", re.I)
TEAM_ANCHOR = re.compile(r"eagles|flyers|phillies|sixers|76ers", re.I)
LOCAL_OUTLETS = {"6abc Action News", "WHYY", "PhillyVoice"}
DEDUP_JACCARD = 0.4

EVENT_BIGRAMS = ["school shooting", "mass shooting", "plane crash",
                 "train derailment", "building collapse", "bridge collapse",
                 "evacuation order", "state of emergency", "ruled out",
                 "out for season", "contract extension", "software update",
                 "product recall", "price cut", "price increase",
                 "power outage", "water main break"]


def proper_nouns(title):
    out = set()
    for w in re.findall(r"[A-Z][a-zA-Z']{2,}", title):
        w = norm_word(w.lower())
        if w not in STOPWORDS:
            out.add(w)
    return out


def event_bigram(text):
    for b in EVENT_BIGRAMS:
        if b in text:
            return b
    return None

# ---------------------------------------------------------------- helpers

def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return htmlmod.unescape(s)


def norm_word(w):
    """Light normalization so 'Hurts'' / 'Hurts' and 'clots' / 'clot' match."""
    w = w.rstrip("'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def tokens(text):
    toks = re.findall(r"[a-z0-9']+", text.lower())
    return set(norm_word(t) for t in toks if t not in STOPWORDS and len(t) > 2)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def phrase_re(p):
    return r"\b" + re.escape(p.strip()) + r"(s|es|ed|d)?\b"


def casualty_count(text):
    """Best-effort death toll from headline phrasing."""
    total = 0
    num = r"(?<![a-z0-9])(\d+)(?![a-z0-9])"
    for m in re.finditer(
            num + r"\s+(children|child|adults|adult|people|students|victims|"
            r"civilians|passengers|residents|others|workers)\b", text):
        window = text[m.end():m.end() + 40]
        if re.search(r"killed|\bdead\b|deaths|fatalities|\bdied\b", window):
            total += int(m.group(1))
    for m in re.finditer(num + r"\s+(?:\w+\s+){0,2}?(killed|\bdead\b|deaths|fatalities)", text):
        total = max(total, int(m.group(1)))
    m = re.search(r"death toll (?:rises? to|of|at) (\d+)", text)
    if m:
        total = max(total, int(m.group(1)))
    return total


def score_title(title_lower, cat):
    """Score a headline in one lane. Returns (score, tiers_hit)."""
    for p in GLOBAL_VETO:
        if re.search(phrase_re(p), title_lower):
            return -1000, set()
    for p in LANE_VETO.get(cat, []):
        if re.search(phrase_re(p), title_lower):
            return -1000, set()
    total = 0
    tiers = set()
    for phrases, pts in KEYWORDS[cat]:
        for p in phrases:
            if p == "breaking" and "breaking down" in title_lower:
                continue
            if re.search(phrase_re(p), title_lower):
                total += pts
                tiers.add(pts)
                break  # count each tier once per story
    if cat == "breaking":
        n = casualty_count(title_lower)
        if n >= 10:
            total += 80
        elif n >= 4:
            total += 50
        elif n >= 1:
            total += 20
    return total, tiers


def qualifies(cat, tiers, casualties, title_lower):
    """The news-event gate: a headline must describe an actual event."""
    if cat == "breaking":
        if any(p in title_lower for p in SMALL_AIRCRAFT) and casualties < 5:
            return False
        return bool(tiers & {120, 60}) or casualties >= 3
    if cat == "local":
        if any(p in title_lower for p in ROUTINE_LOCAL) and casualties < 2:
            return False
        return 80 in tiers or casualties >= 2
    # sports / tesla / ai: top-tier event keyword required
    return 80 in tiers


def guess_team(text, source_team):
    if source_team:
        return source_team
    for team in TEAM_EMOJI:
        if team in text:
            return team
    if "76ers" in text or "sixers" in text:
        return "sixers"
    return None


def allowed_lanes(src, title):
    """Lanes a story from this source may compete in (v2 lane lock)."""
    lanes = {src["cat"]}
    if src["cat"] in ("breaking", "local"):
        lanes.add("breaking")
    if TEAM_ANCHOR.search(title) and src["cat"] in ("breaking", "local", "sports"):
        lanes.add("sports")
    return lanes


def lane_eligible(cat, title, src):
    """Within an allowed lane, the story must genuinely belong there."""
    if cat == "breaking":
        # PBS /show/ items are TV feature segments, not headlines
        return "/newshour/show/" not in (src.get("_link") or "")
    if cat == "tesla":
        return "tesla" in title.lower()
    if cat == "local":
        return bool(PHILLY_ANCHOR.search(title)) or src["name"] in LOCAL_OUTLETS
    if cat == "sports":
        return bool(TEAM_ANCHOR.search(title)) or bool(src.get("team"))
    return True


def parse_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        pp = entry.get(key)
        if pp:
            try:
                return datetime(*pp[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None

# ---------------------------------------------------------------- main

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"stories": {}, "feeds": {}}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=1)


def fetch_source(src, feed_cache):
    url = src["url"]
    headers = dict(UA)
    cached = feed_cache.get(url, {})
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("modified"):
        headers["If-Modified-Since"] = cached["modified"]
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
    except Exception as ex:
        return ("error", f"{type(ex).__name__}: {ex}")
    if r.status_code == 304:
        return ("not_modified", None)
    if r.status_code != 200:
        return ("error", f"HTTP {r.status_code}")
    feed_cache[url] = {"etag": r.headers.get("ETag", ""),
                       "modified": r.headers.get("Last-Modified", "")}
    d = feedparser.parse(r.content)
    return ("ok", d.entries)


def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    stories = state.setdefault("stories", {})
    feed_cache = state.setdefault("feeds", {})

    fetched = 0
    errors = []
    for src in SOURCES:
        status, payload = fetch_source(src, feed_cache)
        if status == "error":
            errors.append(f"{src['name']}: {payload}")
            continue
        if status == "not_modified":
            continue
        for entry in payload:
            title = strip_html(entry.get("title", "")).strip()
            if not title:
                continue
            if src.get("strip_suffix"):
                title = re.sub(r"\s+-\s+[^-]{1,60}$", "", title).strip()
            link = entry.get("link", "").strip()
            summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
            text = (title + " " + summary).lower()
            if src.get("tesla_only") and "tesla" not in text:
                continue

            pub = parse_time(entry) or now
            if pub > now:
                pub = now
            pub_age_h = (now - pub).total_seconds() / 3600

            # v2: publication age is a hard gate, judged per allowed lane
            lanes = allowed_lanes(src, title)
            lanes = {c for c in lanes if pub_age_h <= MAX_PUB_AGE[c]}
            if not lanes:
                continue
            src2 = dict(src, _link=link)

            # score the HEADLINE in each allowed, eligible lane; keep the
            # best lane that also passes the news-event gate
            title_lower = title.lower()
            casualties = casualty_count(title_lower)
            best_cat, base, best_tiers = None, -1000, set()
            for c in lanes:
                if not lane_eligible(c, title, src2):
                    continue
                sc, tiers = score_title(title_lower, c)
                if sc <= -1000:
                    continue
                if not qualifies(c, tiers, casualties, title_lower):
                    continue
                if sc > base:
                    best_cat, base, best_tiers = c, sc, tiers
            if best_cat is None:
                continue
            gate = 120 if 120 in best_tiers else 60 if 60 in best_tiers else 0
            # point-release changelogs (v14.3.13, "(Update 2026.38.5.10)")
            # are maintenance noise unless real outlets cover them
            changelog = bool(re.search(r"v\d+\.\d+\.\d+", title_lower)
                             or re.search(r"\(update\s+20\d\d\.", title_lower))

            # dedupe against stories still within their publication window
            toks = tokens(title)
            proper = proper_nouns(title)
            bigram = event_bigram(title_lower)
            match_id = None
            for sid, s in stories.items():
                s_pub_age = (now - datetime.fromisoformat(s["pub"])).total_seconds() / 3600
                if s_pub_age > MAX_PUB_AGE.get(s["cat"], 48) + 12:
                    continue
                st = s["title"]
                j = jaccard(toks, tokens(st))
                if j >= DEDUP_JACCARD:
                    match_id = sid
                    break
                if j >= 0.25:
                    if bigram and bigram == event_bigram(st.lower()):
                        match_id = sid
                        break
                    if len(proper & proper_nouns(st)) >= 3:
                        match_id = sid
                        break

            if match_id:
                s = stories[match_id]
                s["sources"] = sorted(set(s["sources"]) | {src["name"]})
                s["last_seen"] = now.isoformat()
                s["gate"] = max(s.get("gate", 60), gate)
                s["cas"] = max(s.get("cas", 0), casualties)
                s["changelog"] = bool(s.get("changelog")) or changelog
                if base > s["base"]:
                    s.update({"base": base, "cat": best_cat, "title": title,
                              "link": link,
                              "pub": min(s["pub"], pub.isoformat()),
                              "team": guess_team(title_lower, src.get("team"))})
            else:
                sid = f"s{int(now.timestamp())}_{fetched}"
                stories[sid] = {
                    "title": title, "link": link,
                    "sources": [src["name"]], "cat": best_cat, "base": base,
                    "pub": pub.isoformat(), "first_seen": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "team": guess_team(title_lower, src.get("team")),
                    "trust": src.get("trust", 2),
                    "gate": gate, "cas": casualties,
                    "changelog": changelog,
                }
            fetched += 1

    # story-vs-story merge: fold duplicates that slipped through in one run
    def fingerprint(st):
        t = st["title"]
        return tokens(t), proper_nouns(t), event_bigram(t.lower())

    def same_story(a, b):
        ta, pa, ba = fingerprint(a)
        tb, pb, bb = fingerprint(b)
        j = jaccard(ta, tb)
        return (j >= DEDUP_JACCARD
                or (j >= 0.25 and ba and ba == bb)
                or (j >= 0.25 and len(pa & pb) >= 3))

    def stronger(a, b):
        return a if (a["base"] > b["base"]
                     or (a["base"] == b["base"]
                         and a["first_seen"] <= b["first_seen"])) else b

    sids = [sid for sid, s in stories.items()
            if (now - datetime.fromisoformat(s["pub"])).total_seconds() / 3600
            <= MAX_PUB_AGE.get(s["cat"], 48) + 12]
    drop = set()
    for i, aid in enumerate(sids):
        if aid in drop:
            continue
        for bid in sids[i + 1:]:
            if bid in drop:
                continue
            A, B = stories[aid], stories[bid]
            if not same_story(A, B):
                continue
            K = stronger(A, B)
            D = B if K is A else A
            K["sources"] = sorted(set(K["sources"]) | set(D["sources"]))
            K["first_seen"] = min(K["first_seen"], D["first_seen"])
            K["last_seen"] = max(K["last_seen"], D["last_seen"])
            K["pub"] = min(K["pub"], D["pub"])
            K["gate"] = max(K.get("gate", 60), D.get("gate", 60))
            K["cas"] = max(K.get("cas", 0), D.get("cas", 0))
            K["changelog"] = bool(K.get("changelog")) or bool(D.get("changelog"))
            drop.add(aid if K is B else bid)
    for sid in drop:
        del stories[sid]

    # prune: publication age beyond the lane limit (+12h grace) is dead,
    # no matter how often a source re-lists it; last_seen backstop 72h
    for sid in [s for s in stories
                if (now - datetime.fromisoformat(stories[s]["pub"])).total_seconds() / 3600
                > MAX_PUB_AGE.get(stories[s]["cat"], 48) + 12
                or (now - datetime.fromisoformat(stories[s]["last_seen"])).total_seconds() / 3600 > 72]:
        del stories[sid]

    # rank: importance x recency, with age measured from PUBLICATION
    ranked = []
    for sid, s in stories.items():
        pub_age_h = max(0.05, (now - datetime.fromisoformat(s["pub"])).total_seconds() / 3600)
        if pub_age_h > MAX_PUB_AGE.get(s["cat"], 48):
            continue
        # Significance (v3): coverage is counted in INDEPENDENT publishers.
        # Google News search results are echoes of one article, not
        # corroboration. A weak story is one nobody else is carrying.
        indep = [x for x in s["sources"] if not x.startswith("Google News")]
        if not (len(indep) >= 2 or (len(indep) == 1 and s.get("trust", 2) >= 3)
                or s.get("cas", 0) >= 4):
            continue
        if s.get("changelog") and len(indep) < 2:
            continue  # point-release notes need real coverage to matter
        if s["cat"] == "breaking":
            # "True breaking" needs weight behind it: a top-tier event,
            # a serious death toll in the headline, or multiple independent
            # outlets carrying it. A lone report of a mid-tier event is
            # not enough.
            if not (s.get("gate", 60) >= 120 or s.get("cas", 0) >= 4
                    or len(indep) >= 2):
                continue
        decay = 0.5 ** (pub_age_h / HALF_LIFE.get(s["cat"], 16))
        corroboration = min(30, 15 * (len(s["sources"]) - 1))
        final = (s["base"] + corroboration) * decay + s.get("trust", 2)
        s["_final"] = round(final, 1)
        s["_age_h"] = round(pub_age_h, 1)
        if final >= THRESHOLD.get(s["cat"], 45):
            ranked.append((final, sid))
    ranked.sort(reverse=True)

    picked, counts = [], {c: 0 for c in CAT_CAP}
    for final, sid in ranked:
        c = stories[sid]["cat"]
        if counts[c] < CAT_CAP[c]:
            picked.append((final, sid))
            counts[c] += 1
        if len(picked) >= MAX_ITEMS:
            break

    write_rss(picked, stories, now)

    print(f"fetched_items={fetched} live_stories={len(stories)} picked={len(picked)}")
    for final, sid in picked:
        s = stories[sid]
        print(f"  [{s['cat']:8s}] {final:6.1f} (pub {s['_age_h']}h, {len(s['sources'])} src) {s['title'][:95]}")
    for e in errors:
        print(f"  ERROR: {e}", file=sys.stderr)

    for s in stories.values():
        s.pop("_final", None)
        s.pop("_age_h", None)
    save_state(state)


def write_rss(picked, stories, now):
    items_xml = []
    for final, sid in picked:
        s = stories[sid]
        team = s.get("team")
        emoji = TEAM_EMOJI.get(team, EMOJI.get(s["cat"], "")) if s["cat"] == "sports" else EMOJI.get(s["cat"], "")
        title = f"{emoji} {s['title']}".strip()
        pub_dt = datetime.fromisoformat(s["pub"])
        local_dt = pub_dt.astimezone(ET)
        desc = f"{', '.join(s['sources'])} - {local_dt.strftime('%b %d, %I:%M %p ET')}"
        items_xml.append(
            "    <item>\n"
            f"      <title>{xml_escape(title)}</title>\n"
            f"      <link>{xml_escape(s['link'])}</link>\n"
            f"      <guid isPermaLink=\"true\">{xml_escape(s['link'])}</guid>\n"
            f"      <pubDate>{format_datetime(pub_dt)}</pubDate>\n"
            f"      <category>{s['cat']}</category>\n"
            f"      <description>{xml_escape(desc)}</description>\n"
            "    </item>")

    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        '  <channel>\n'
        '    <title>Sal\'s Feed</title>\n'
        '    <description>Only the stories worth your time: major breaking news, '
        'Philadelphia headlines, Philly sports, Tesla owner news, and AI developments.</description>\n'
        '    <language>en-us</language>\n'
        f'    <lastBuildDate>{format_datetime(now)}</lastBuildDate>\n'
        '    <ttl>20</ttl>\n'
        + "\n".join(items_xml) + "\n"
        '  </channel>\n'
        '</rss>\n')
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        f.write(rss)


def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


if __name__ == "__main__":
    main()
