# Sal's Feed

A free personal RSS aggregator. Every 20 minutes it pulls ~25 reputable RSS
feeds across four lanes — major breaking news, Philadelphia local news,
Philly sports (Eagles, Flyers, Phillies, Sixers), and Tesla owner news —
scores each story for genuine significance, deduplicates across outlets,
ranks by importance × recency, and publishes the ~6–10 stories actually
worth reading as a standard RSS 2.0 feed.

**The feed:** `https://<your-username>.github.io/sals-feed/feed.xml`

Paste that URL into any RSS reader or widget.

## How it works

- `aggregate.py` fetches the feeds (with conditional GETs to be polite),
  scores stories with tiered keyword rules per lane, and vetoes the junk:
  stock chatter, rumors, gossip, routine crime, game recaps, promo spam.
- Stories reported by multiple outlets get corroboration boosts and merge
  into a single entry (best version wins).
- Scores decay with age, so big new stories jump to the top and old ones
  fade out. Per-lane caps keep one topic from hogging the feed.
- Seen-story state lives in `state.json`, committed each run so nothing
  is lost between runs.

## Cost

$0. GitHub Actions (public repo = unlimited minutes) + GitHub Pages.
No paid RSS service, no server, nothing running on your Mac.

## Tweaking

Edit the `KEYWORDS`, `THRESHOLD`, or `SOURCES` sections at the top of
`aggregate.py`, commit, and the next scheduled run picks it up.
