#!/usr/bin/env python3
"""
Sal's Feed - a free personal RSS aggregator.

Fetches ~25 reputable RSS feeds across four lanes (major breaking news,
Philadelphia local, Philly sports, Tesla), scores each story for genuine
significance, deduplicates across outlets, ranks by importance x recency,
and emits a tight RSS 2.0 feed of the ~6-10 stories actually worth reading.

Runs on GitHub Actions (free) every 20 minutes; GitHub Pages serves feed.xml.
State (seen stories, HTTP caches) persists in state.json in the repo.
"""
import json
import os
import re
import sys
import time
import html as htmlmod
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

import feedparser
import requests

# ---------------------------------------------------------------- config

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
FEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.xml")

UA = {"User-Agent": "SalsFeedBot/1.0 (personal RSS aggregator, polling every 20 min)"}
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
]

# (phrase, points). Matched case-insensitively against title + summary text.
KEYWORDS = {
    "breaking": [
        (["terrorist attack", "terror attack", "assassination", "assassinated",
          "plane crash", "airliner", "passenger jet",
          "rocket explod", "nuclear ", "invasion", "invades", "declare war",
          "declares war", "missile strike", "military coup", "martial law",
          "state of emergency"], 120),
        (["mass shooting", "school shooting",
          "earthquake", "hurricane", "tornado emergency", "tsunami",
          "volcano eruption", "major wildfire", "explosion", "building collapse",
          "bridge collapse", "train derail", "hostage", "evacuation order",
          "evacuations ordered", "supreme court", "strikes down",
          "deadly shooting", "plane down"], 60),
        (["breaking", "emergency", "killed", "dead", "deadly", "resigns", "dies at",
          "passed away", "indicted", "crash", "crashed", "erupts"], 30),
        (["opinion", "editorial", "op-ed", "review:", "best ", "how to ",
          "recipe", "horoscope", "crossword", "kardashian", "taylor swift",
          "bachelor", "real housewives", "sponsored", "gift guide"], -1000),
        (["prevented", "averted", "foiled", "possible mass", "plot to"], -80),
        (["heartbreak", "one family's", "quiz", "test identifies", "essay:",
          "memoir", "what it was like"], -70),
    ],
    "local": [
        (["tornado warning", "tornado emergency", "flash flood",
          "severe thunderstorm warning", "septa", "phl airport",
          "amtrak", "i-95", "schuylkill", "evacuation", "state of emergency",
          "without power", "power outage", "water main", "bridge collapse",
          "bridge closed"], 80),
        (["mayor", "airport", "crash", "pileup", "shutdown", "strike", "school district",
          "city council", "infrastructure", "transit", "delaware county",
          "shooting", "stabbing", "fire"], 35),
        (["best restaurants", "now open", "for sale", "housing market",
          "home prices", "gift guide", "sponsored", "spotted", "gossip"], -60),
    ],
    "sports": [
        (["out for season", "season-ending", "torn acl", "achilles", "injured reserve",
          "trade", "traded", "acquires", "signs ", "signing", "contract extension",
          "agree to deal", "fired", "firing", "part ways", "head coach",
          "general manager", "playoff", "clinch", "eliminated", "suspended",
          "arrest", "retire", "retirement", "blood clot",
          "winning streak", "losing streak", "win streak"], 80),
        (["injury", "injuries", "injured", "questionable", "doubtful", "will miss", "ruled out",
          "roster move", "waived", "released", "depth chart", "starting"], 30),
        (["promo code", "prizepicks", "polymarket", "fanduel", "draftkings",
          "gamethread", "live chat", "how to watch", "game thread"], -80),
        (["game recap", "final score", "preview", "odds", "betting", "fantasy",
          "mock draft", "power rankings", "mailbag", "podcast"], -40),
        (["rumor", "rumors", "rumored", "speculation"], -50),
    ],
    "tesla": [
        (["recall", "software update", "fsd", "full self-driving", "robotaxi",
          "supercharger", "new model", "price cut", "price increase",
          "raises prices", "lowers prices", "cuts prices", "release notes",
          "hardware 5", "ai5"], 80),
        (["update", "version 20", "model y", "model 3", "cybertruck", "semi",
          "roadster", "range", "charging"], 30),
        (["stock", "tsla", "shares ", "shareholder", "analyst", "price target",
          "earnings", "grok", "spacex", "neuralink", "twitter", "sponsored"], -1000),
        (["rumor", "render", "concept", "musk's pay", "compensation package"], -60),
    ],
}

HALF_LIFE = {"breaking": 7, "local": 10, "sports": 12, "tesla": 16}
THRESHOLD = {"breaking": 55, "local": 45, "sports": 45, "tesla": 45}
CAT_CAP = {"breaking": 4, "local": 3, "sports": 3, "tesla": 2}
MAX_ITEMS = 10
STORY_TTL_HOURS = 36

EMOJI = {"breaking": "\U0001F30D", "local": "\U0001F3D9\uFE0F",
         "sports": "\U0001F3DF\uFE0F", "tesla": "\u26A1"}
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

# A story only counts as "local" if it is actually about the Philly area,
# and as "sports" if it is actually about a Philly team. (Google News
# searches return national stories too; this keeps lanes honest.)
PHILLY_ANCHOR = re.compile(
    r"philadelphia|philly|delaware county|\bdelco\b|septa|schuylkill|center city|"
    r"south jersey|pennsylvania|new jersey", re.I)
TEAM_ANCHOR = re.compile(r"eagles|flyers|phillies|sixers|76ers", re.I)
LOCAL_OUTLETS = {"6abc Action News", "WHYY", "PhillyVoice"}
DEDUP_JACCARD = 0.4

# Shared "event phrases" let differently-worded headlines about the same
# incident merge (e.g. two wordings of one school shooting).
EVENT_BIGRAMS = ["school shooting", "mass shooting", "plane crash",
                 "train derailment", "building collapse", "bridge collapse",
                 "evacuation order", "state of emergency", "ruled out",
                 "out for season", "contract extension", "software update",
                 "product recall", "price cut", "price increase",
                 "power outage", "water main break"]


def proper_nouns(title):
    out = set()
    for w in re.findall(r"[A-Z][a-zA-Z']{2,}", title):
        if w.lower() not in STOPWORDS:
            out.add(w.lower())
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


def tokens(text):
    toks = re.findall(r"[a-z0-9']+", text.lower())
    return set(t for t in toks if t not in STOPWORDS and len(t) > 2)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def phrase_re(p):
    # word-boundary match, tolerant of plural/past-tense endings
    return r"\b" + re.escape(p.strip()) + r"(s|es|ed|d)?\b"


def casualty_count(text):
    """Best-effort death toll from phrasing like '3 children, 1 adult killed'."""
    total = 0
    # standalone numbers only: "NBC10" or "6abc" must not count as casualties
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


def score_text(text, cat, detail=False):
    total = 0
    matched = []
    for phrases, pts in KEYWORDS[cat]:
        for p in phrases:
            if p == "breaking" and "breaking down" in text:
                continue  # "breaking down the film", not breaking news
            if re.search(phrase_re(p), text):
                if pts <= -1000:
                    return (-1000, ["VETO:" + p]) if detail else -1000
                total += pts
                matched.append(f"{p}({pts})")
                break  # count each tier once per story
    if cat == "breaking":
        n = casualty_count(text)
        if n >= 10:
            total += 80
            matched.append(f"casualties~{n}(+80)")
        elif n >= 4:
            total += 50
            matched.append(f"casualties~{n}(+50)")
        elif n >= 1:
            total += 20
            matched.append(f"casualties~{n}(+20)")
    return (total, matched) if detail else total


def guess_team(text, source_team):
    if source_team:
        return source_team
    for team in TEAM_EMOJI:
        if team in text:
            return team
    if "76ers" in text or "sixers" in text:
        return "sixers"
    return None


def lane_allowed(cat, text, src):
    """A story can only win a lane if it genuinely belongs there."""
    if cat == "breaking":
        return True
    if cat == "tesla":
        return "tesla" in text
    if cat == "local":
        return bool(PHILLY_ANCHOR.search(text)) or src["name"] in LOCAL_OUTLETS
    if cat == "sports":
        return (bool(TEAM_ANCHOR.search(text)) or src.get("team")
                or src["name"] == "Crossing Broad")
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
                # Google News appends " - Source Name" to every headline
                title = re.sub(r"\s+-\s+[^-]{1,60}$", "", title).strip()
            link = entry.get("link", "").strip()
            summary = strip_html(entry.get("summary", "") or entry.get("description", ""))
            text = (title + " " + summary).lower()
            if src.get("tesla_only") and "tesla" not in text:
                continue

            # score in every lane; the story takes its best *eligible* lane
            scored = {c: (score_text(text, c) if lane_allowed(c, text, src) else -1000)
                      for c in KEYWORDS}
            best_cat = max(scored, key=scored.get)
            base = scored[best_cat]
            if base <= -1000:
                continue

            pub = parse_time(entry) or now
            # stories fresher than the feeds' own clocks still count as now
            if pub > now:
                pub = now

            # dedupe: find an existing story telling the same tale.
            # Fingerprints are recomputed from each story's current (best)
            # title every time, so they never go stale.
            toks = tokens(title)
            proper = proper_nouns(title)
            bigram = event_bigram(title.lower())
            match_id = None
            for sid, s in stories.items():
                age_h = (now - datetime.fromisoformat(s["first_seen"])).total_seconds() / 3600
                if age_h > 48:
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
                if base > s["base"]:
                    s.update({"base": base, "cat": best_cat, "title": title,
                              "link": link, "pub": pub.isoformat(),
                              "team": guess_team(text, src.get("team"))})
            else:
                sid = f"s{int(now.timestamp())}_{fetched}"
                stories[sid] = {
                    "title": title, "link": link,
                    "sources": [src["name"]], "cat": best_cat, "base": base,
                    "pub": pub.isoformat(), "first_seen": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "team": guess_team(text, src.get("team")),
                    "trust": src.get("trust", 2),
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
            if (now - datetime.fromisoformat(s["first_seen"])).total_seconds() / 3600 <= 48]
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
            drop.add(aid if K is B else bid)
    for sid in drop:
        del stories[sid]

    # prune stale stories
    for sid in [s for s in stories
                if (now - datetime.fromisoformat(stories[s]["last_seen"])).total_seconds() / 3600 > STORY_TTL_HOURS]:
        del stories[sid]
    # keep state.json small: drop weak, uncorroborated stories after 6h
    for sid in [s for s in stories
                if (now - datetime.fromisoformat(stories[s]["first_seen"])).total_seconds() / 3600 > 6
                and stories[s]["base"] < 30 and len(stories[s]["sources"]) < 2]:
        del stories[sid]

    # rank: importance x recency
    ranked = []
    for sid, s in stories.items():
        age_h = max(0.05, (now - datetime.fromisoformat(s["first_seen"])).total_seconds() / 3600)
        decay = 0.5 ** (age_h / HALF_LIFE.get(s["cat"], 10))
        corroboration = min(90, 30 * (len(s["sources"]) - 1))
        fresh = 1.25 if (age_h < 1 and s["cat"] in ("breaking", "local")) else 1.0
        final = (s["base"] + corroboration) * decay * fresh + s.get("trust", 2)
        s["_final"] = round(final, 1)
        s["_age_h"] = round(age_h, 1)
        if final >= THRESHOLD.get(s["cat"], 45):
            ranked.append((final, sid))
    ranked.sort(reverse=True)

    # per-category caps, then overall cap
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
        print(f"  [{s['cat']:8s}] {final:6.1f} ({s['_age_h']}h, {len(s['sources'])} src) {s['title'][:95]}")
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
        'Philadelphia headlines, Philly sports, and Tesla owner news.</description>\n'
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
