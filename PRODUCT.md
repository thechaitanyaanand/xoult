# Product notes

## Who it's for

ML/NLP/IR researchers and engineers who follow arXiv. cs.CL, cs.IR and cs.LG alone
announce over a thousand new papers on a typical weekday. Nobody reads that list; people
skim titles, rely on Twitter, or miss things.

## The job

"Show me the handful of today's papers I'd actually want to read, and tell me in one line
why each one matters, so I can decide in seconds."

## What success looks like

- **Save rate**: saves / impressions, per ranker version. This is the north-star metric.
  Clicks are logged too, but a click on a misleading title isn't success.
- **Dismiss rate** going down over a user's first two weeks: the feed is learning.
- Offline: NDCG@10 on CiteULike must not regress (enforced in CI).

Showing more papers is not a goal. A feed that shows 20 and gets 3 saves beats one that
shows 200 and gets 5.

## Decisions and why

- **Two buttons, not a 5-star rating.** Save / not-for-me is one tap and gives a clean
  positive and negative signal. Ratings get skipped.
- **Ask for categories at signup.** A new user has no history to personalize from (cold
  start). Picking categories takes five seconds and makes the first feed reasonable.
- **TLDRs are generated once per paper, not per user.** LLM cost scales with arXiv's
  volume, not with the number of users, so the free tiers hold.
- **Show the abstract one click away.** A TLDR can be wrong. The source is always visible,
  and the TLDR never replaces it.
- **No account system yet.** A random id in the browser is enough to test whether the
  ranking is useful. Logins come after people want to use it on two devices.

## Deliberately not built (yet)

| Not built | Why | Build it when |
|---|---|---|
| Email digest | Needs accounts and deliverability work | Users return daily on their own first |
| Full-text search | Different job (lookup, not discovery) | Users ask for it |
| Fine-tuned ranker | Not enough interaction data to train one | Thousands of saves logged |
| Diversity re-ranking (MMR) | Unclear the feed is too repetitive | Dismiss data shows near-duplicate fatigue |
| Backfill of missed days | Cron has been reliable | A skipped run actually loses papers |
