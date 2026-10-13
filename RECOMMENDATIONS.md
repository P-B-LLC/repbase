# The For You page

How recommendations are made, how they keep up with the reader, how to tune
and test changes to them, what the reader controls, what is kept about them,
and what a client has to do. The ranking is `core/recommendations.py`, the
people suggested beside it are `core/suggestions.py`, and the endpoints are in
`core/for_you.py`.

## Design in one paragraph

Rules, not a model. There is not yet enough interaction data to train anything
trustworthy, and a ranking nobody can read cannot be debugged or explained to
a user. So each reader gets an *interest profile* built from what they did and
said, with what they did in the last few hours kept apart as *momentum*; each
eligible recent post gets a *score* made of named parts with configurable
weights; and the scored posts are *re-ranked* so no creator or topic takes the
page over and one slot in five goes to something new. Each later page is
re-scored against a fresh profile, so the page keeps up with the reader while
they scroll. Every post on the page says why it is there, and the reader can
see what the page has picked up about them, hide topics, start fresh, or turn
personalisation off.

## Signals

| Signal | Source | Weight (default) |
| --- | --- | --- |
| Like | `PostLike` | +1.0 |
| Comment | `PostComment` | +2.0 |
| Save (workout or meal copied) | `WorkoutTemplate.source_post`, `SavedFoodMeal.source_post` | +3.0 |
| Share (repost) | `Post.repost_of` | +2.5 |
| Follow | `Follow` | +3.0 to the author |
| Stayed (average view ≥ 5 s) | `PostView.total_dwell_ms / view_count` | +0.5 |
| Watched at least half of a clip | `PostView.max_watch_ms` against the clip's length | +0.5 |
| Watched a clip to the end | `PostView.completed` | +1.0 |
| Scrolled past (< 1.5 s, no watching) | `PostView.skipped` | −0.3 |
| Not interested | `PostFeedback` | −4.0 (×0.75 for the author) |
| Named during onboarding, or a profile discipline | `Personalization`, `UserDiscipline` | +2.0 per topic |

Interactions from the last 90 days count, at most 300 of each kind, each
halving in weight every 21 days. A post's **topics** are its kind, its workout
type or planner category, whether it carries a clip, and -- at half weight --
its author's disciplines. A signal's weight is spread over the topics of the
post it was about, and over its author. Totals are squashed into −1…1 with
`tanh`, so a few signals move a topic a lot and many more move it only a
little further, which is how interest actually saturates.

**Momentum** is the same signals from the last six hours alone, undecayed and
squashed more steeply (two likes this afternoon count for nearly as much as
momentum can). It is what makes the page lean toward what somebody is into
*tonight* without forgetting what they were into last month.

Onboarding answers map to topics: Strength → lifting and the strength
disciplines; Running, Cycling, Swimming → their workout type and discipline
(plus triathlon); Nutrition → meals; Training → workouts.

## Pipeline

1. **Profile** (`build_profile`): the signals above, read with bounded
   queries, plus how much having *seen* each post counts against it.
2. **Eligibility** (`eligible_posts`): built on `visible_posts_for`, so a post
   the reader could not open is never a candidate -- hidden, blocked either
   way, private, followers-only without a follow, closed profile, suspended
   author. On top of that it removes the reader's own posts, reposts (the
   original is the candidate), anything older than 30 days, posts the reader
   reported or dismissed, **posts they already liked, commented on, saved or
   reposted**, posts on **topics they hid**, posts whose clip is waiting for or
   failed review, and posts with three or more open reports.
3. **Candidates** (`candidates`): five capped sources, merged --
   - **fresh**: the newest 500 eligible posts;
   - **following**: the newest 150 from people followed;
   - **interest**: the newest 150 on the reader's five strongest topics
     (momentum included);
   - **similar**: up to 150 posts liked by the reader's taste neighbours (below);
   - **trending**: up to 150 of the 300 posts with the most likes this week,
     worked out once for everyone every five minutes.

   **Similar taste.** The reader's latest positive actions (up to 100 posts)
   are matched against everyone else's likes. People sharing at least two of
   them are weighed by cosine similarity -- shared likes over the geometric
   mean of both histories, so somebody who likes everything is not everybody's
   twin -- and the 50 most alike count. Their recent likes are the candidates,
   each scored by the similarity of the people who liked it, relative to the
   best, and scaled down when fewer than five neighbours were found. Blocked
   and suspended accounts are nobody's neighbour.
4. **Score** (`score`):

   | Part | Meaning | Weight |
   | --- | --- | --- |
   | topic | weighted mean of the reader's affinity for the post's topics | 2.0 |
   | momentum | the same, against the last six hours only | 1.5 |
   | author | the reader's affinity for the author | 1.5 |
   | follows | the reader follows the author | 1.0 |
   | similar | how strongly people with the reader's taste liked it (0–1) | 1.2 |
   | quality | engagement per view, smoothed toward 0.1 over 20 views, *explored* | 1.0 |
   | popularity | log-scaled engagement (likes + 2·comments + 3·saves + 2·shares) | 0.6 |
   | recency | halves every 36 hours | 1.2 |
   | watch | share of views that stayed (or finished the clip), smoothed | 0.6 |
   | newcomer | author joined in the last 60 days, fading as followers arrive (0 at 30) | 0.4 |
   | seen | how much having seen it counts (below) | −3.0 |
   | reported | open reports below the exclusion threshold | −1.0 |
   | jitter | tiny noise, so ties do not always break the same way | 0.02 |

   **Exploration.** Quality is not pinned to a post's average: it is drawn
   from the gamma posterior its likes and views allow (Thompson sampling), and
   moved `explore` (0.5) of the way from the average toward that draw. A post
   nobody has seen yet moves by about ±0.09, one with a hundred views by about
   ±0.03. That is what gives a new post a chance to show what it is without
   letting chance outweigh what a reader said they like
   (`test_interest_wins_whatever_the_dice_say` holds that line across seeds).

   **Seen.** Every post the reader viewed in the last 30 days counts against
   itself: fully when just seen, half after two days, a quarter after four;
   up to twice as much when seen repeatedly, and half as much again when it
   was scrolled straight past. Something seen last week can come back;
   something seen four times today does not.
5. **Rank** (`rank`): highest adjusted score first, where each earlier
   appearance of the same author costs 1.0 and each recent appearance of the
   same main topic costs 0.35; the same author never appears twice within four
   posts while anyone else is available; and every fifth slot is **discovery**:
   one of the best few posts by an author the reader has never interacted with,
   on a topic outside their top three, never one they have signalled against.
   These rules carry across page boundaries (`RankState`).
6. **Explain** (`_reason`), most personal first:

   | Reason | Label |
   | --- | --- |
   | `discovery` | Something new for you |
   | `following` | From someone you follow |
   | `interest` | Because you like *topic* |
   | `similar` | Liked by people with similar taste -- only when at least two of them did |
   | `new_creator` | From a new creator |
   | `popular` | Popular in the community |
   | `fresh` | New in the community |

   `reason` is a plain string on the wire. A client meeting one it does not
   know shows the label.

## Keeping up while somebody scrolls

A ranking is not an `ORDER BY`, so each For You visit is a *session*, kept in
the cache for 30 minutes under the reader's id and a random name, and paged by
position with a signed cursor. A session holds what it has served, in order,
and the candidates it has not served yet.

- The **first page** (no cursor) builds a profile, finds candidates and ranks
  the first page.
- Each **later page** builds a fresh profile, re-checks the remaining
  candidates against the eligibility rules, re-scores them and ranks the next
  page from them. So the posts a reader liked, dismissed or scrolled past on
  page one have already shaped page two, and a post that became hidden,
  deleted, blocked or liked in the meantime simply is not ranked: later pages
  stay full.
- What has been served is never re-ranked, so a position always means the same
  post: a retried request gets the same page, and paging never repeats or
  skips a post.
- Each page is still re-checked against the visibility rules and the reader's
  dismissals as it is served.
- A session serves at most 200 posts; a pull to refresh (no cursor) ranks
  afresh. An expired session starts a new ranking from the top rather than
  resuming at its old position -- position 40 of a different ranking is not a
  continuation of anything, and what was already seen is pushed down by having
  been seen.

The cursor is signed, so it cannot be forged, and the cache key includes the
reader, so a cursor from someone else opens nothing of theirs.

## New accounts, new posts, new creators

With no history, the onboarding answers and profile disciplines are a prior
that personalises the very first page. With nothing at all, topic and author
parts are zero, so the page is ordered by quality, popularity and freshness,
and the diversity rules and discovery slots still apply: a varied page of the
best of what is new. The first like starts making it personal -- on the very
next page.

A new *post* is helped by freshness and by exploration; a new *creator* -- an
account under 60 days old with fewer than 30 followers -- by the `newcomer`
part, which fades as followers arrive, and by discovery slots, which favour
authors the reader has never met. Trending makes sure a post that took off a
week ago is still a candidate after the newest 500 have moved past it.

## The reader's say

`GET /api/v1/social/for-you/preferences/` shows the reader what the page uses:

- **interests**: the topics it has picked up, strongest first, each with a
  strength from 0 to 1 and whether the reader named it themselves. Topics
  only -- never authors or posts, which would turn "what Rytivo thinks you
  like" into a list of who you look at.
- **hidden_topics** and the **hideable_topics** to choose from: workouts,
  lifting, running, cycling, swimming, meals, planning, videos. A hidden topic
  is never shown -- an exclusion, not a nudge.
- **personalized**: off means the page stops using what the reader does --
  likes, views, follows, onboarding answers -- and orders eligible posts by
  quality, popularity and freshness alone. Posts already seen are still moved
  down, and hidden topics and dismissed posts stay hidden.

`PATCH` changes `personalized` and `hidden_topics`.
`DELETE /api/v1/social/for-you/history/` **starts fresh**: everything before
now stops counting toward the page, and the reader's viewing history is
deleted outright. Likes, comments, follows and "not interested" stay -- they
are the reader's own actions, shown to other people or chosen on purpose, not
a record kept about them -- they simply stop counting.

"Not interested" on any post dismisses it at once, even from a page already
ranked, and can be undone.

## People to follow

`GET /api/v1/social/suggested-people/` suggests up to 20 accounts, each with a
reason:

| Reason | Label | Source |
| --- | --- | --- |
| `engaged` | You often like their posts | authors the reader has a clear affinity for but does not follow |
| `mutual` | Followed by N people you follow | follows of the people the reader follows |
| `similar` | Popular with people who like what you like | authors of the posts the reader's taste neighbours liked |
| `discipline` | Also into *discipline* | people with the reader's disciplines who posted lately |
| `new_creator` | New to Rytivo | accounts under 60 days old with a recent public post |
| `popular` | Popular on Rytivo this week | authors of this week's most liked posts |

Only open, active profiles that posted in the last 60 days are suggested.
People the reader follows, asked to follow, blocked or was blocked by, and
anyone they dismissed in the last six months are left out.
`POST /api/v1/social/suggested-people/{id}/dismiss/` dismisses; `DELETE` takes
it back.

## Privacy

- **What is kept.** For each post that was on a reader's screen: how many
  times, for how long in all, how far into its clip, whether it was finished
  or scrolled past, where it was first seen and, for For You, the reason it was
  shown for. One row per reader and post (`PostView`). Plus "not interested"
  (`PostFeedback`), the reader's choices (`RecommendationPreferences`) and
  dismissed suggestions (`SuggestionDismissal`).
- **For how long.** Viewing history and "not interested" for 90 days -- as long
  as the ranking reads them -- then `prune_expired_rows` (part of
  `python -m config.deploy maintenance`) deletes them. Dismissed suggestions for
  180 days. The deploy check `core.W007` warns when history older than that is
  still there, which means the daily job is not running. Starting fresh deletes
  a reader's viewing history at once; deleting an account deletes all of it.
- **Who sees it.** Nobody. It moves the reader's own page, and other readers'
  pages only through counts smoothed over everybody's views.
- **What a page never reveals.** "Liked by people with similar taste" is only
  said of a post at least two such people liked, so it never stands for one
  identifiable person. Taste neighbours are never suggested as people to follow
  -- that would make their likes the reason. "Followed by N people you follow"
  counts only followers whose own profile is open, so it cannot reveal who a
  closed account follows. Closed profiles are never suggested.
- **Measurement names nobody.** `feed_report` prints counts and rates only;
  experiment arms are worked out from a hash, not stored; the ranking log line
  carries no user identifiers.

## Performance

Every query is bounded by a cap, not by the amount of content: the number of
queries one ranking makes is constant (`ScaleTests` asserts it, for first and
later pages and for suggestions), and the candidate pool is at most 1,100
posts. What is the same for every reader is computed once and shared through
the cache:

- per-post engagement counts (likes, comments, shares, saves, views, long views,
  completions) for 10 minutes -- counting views was most of what a ranking
  cost;
- this week's trending posts for 5 minutes;
- follower counts of recently joined authors, for the newcomer part, for 15
  minutes.

The reader's own actions are always read live, so nothing they just did waits
on a cache.

Measured on PostgreSQL 16 with 20,000 accounts, 90,000 posts (30,000 inside
the window), 900,000 likes, 3.2 million view rows and 400,000 follows, over 30
readers:

| | p50 | p95 | queries |
| --- | --- | --- | --- |
| first page, shared counts cached (normal) | 91 ms | 98 ms | 22 |
| first page, cache empty (after a restart) | 275 ms | 476 ms | 30 |
| later page, re-ranked | 28 ms | 30 ms | 15 |
| people suggestions | 57 ms | 67 ms | 30 |
| the ranking before this version, first page | 149 ms | 162 ms | 16 |

Indexes it relies on: `PostLike(user, -created_at)`, `PostComment(author,
-created_at)`, `PostView(viewer, -last_seen_at)`, `PostView(surface,
first_seen_at)`, `PostFeedback(viewer, -created_at)`, the unique pairs on the
like, view and feedback tables, the foreign-key indexes the counts read, and
`Post(-created_at, -id)`, which the fresh source walks. A later page re-checks
up to 400 remaining candidates; that is most of its cost.

When the community outgrows this -- hundreds of thousands of posts a month --
the next step is a table of per-post counts kept up to date by a scheduled
job, in place of the shared cache.

## Monitoring

- **`Server-Timing: rank;dur=…`** on every page that ranked anything, visible
  in any browser's network panel.
- **A log line per fresh ranking** on the `core.recommendations` logger (INFO;
  later pages at DEBUG): posts ranked from how many candidates, whether the
  reader was new or had personalisation off, the experiment arm, the time
  taken and the mix of reasons. No user identifiers.
- **`python manage.py feed_report [--days 7] [--hours 24] [--json]`**: counts
  and rates only --
  - impressions, viewers and distinct posts and authors shown (coverage);
  - engagement rate: the share of For You views the reader then liked,
    commented on, saved or reposted, and each of those on its own (`actions`);
  - skip rate, not-interested rate and clip completion rate;
  - average dwell, and for clips how far people got and the total watch time
    (`watch`);
  - every rate per reason (`by_reason`), which is how to tell whether
    discovery, similar taste and new creators earn their slots;
  - **retention**: of the people whose first For You view in the window was
    long enough ago to tell, the share who were back the next day and within a
    week (needs `ANALYTICS_ENABLED`, which records daily activity);
  - ranking latency for first and later pages, as histograms with p50/p95
    bounds;
  - per experiment arm, when one is running (`by_variant`).

  Run it before and after changing weights.

## Tuning

Every weight above is a field of `FeedWeights`. Override any of them in
settings:

```python
FOR_YOU_WEIGHTS = {"recency": 1.5, "discovery_every": 4, "explore": 0.3}
```

An unknown name stops the server starting (system check `core.E009`) rather
than being silently ignored. Change one weight at a time and compare
`feed_report` over the same window length -- or better, run it as an
experiment. Signs to watch: a rising skip or not-interested rate means the
page is showing the wrong things; falling coverage means it is concentrating
on fewer creators; a falling engagement rate with stable coverage usually
means recency is outweighing quality.

## Experiments

```python
FOR_YOU_EXPERIMENT = {
    "name": "fresher-2026-11",
    "variants": {"control": {}, "fresher": {"recency": 1.6}},
}
```

Readers are split evenly between the variants by a hash of the experiment's
name and their id: nothing is stored, a reader stays in one arm for the life
of the experiment, and renaming it reshuffles everyone. Each variant's
overrides apply on top of `FOR_YOU_WEIGHTS`. `feed_report` then reports every
figure, return rates included, per arm. A malformed experiment stops the
server starting (`core.E009`). Remove the setting to end it.

## API for clients

```text
GET    /api/v1/social/for-you/?cursor=…&page_size=…        page of posts, each with recommendation {reason, label}
POST   /api/v1/social/impressions/                         {"events": [{post, dwell_ms, watch_ms, completed, surface}]}, ≤ 50
POST   /api/v1/social/posts/{id}/not-interested/           204
DELETE /api/v1/social/posts/{id}/not-interested/           204
GET    /api/v1/social/for-you/preferences/                 {personalized, hidden_topics, history_cleared_at, interests, hideable_topics}
PATCH  /api/v1/social/for-you/preferences/                 {personalized?, hidden_topics?} → the same
DELETE /api/v1/social/for-you/history/                     start fresh → the same
GET    /api/v1/social/suggested-people/?limit=…            [{person, reason, label}]
POST   /api/v1/social/suggested-people/{id}/dismiss/       204
DELETE /api/v1/social/suggested-people/{id}/dismiss/       204
POST   /api/v1/social/posts/{id}/video/                    raw MP4/QuickTime body; returns the post
DELETE /api/v1/social/posts/{id}/video/                    returns the post
```

Writes on the preferences, history and dismissal endpoints share the `social`
rate limit with likes and "not interested".

### What a client should send

- An impression for each post that was at least 60% on screen, with how long
  (`dwell_ms`), and for a clip how far it got (`watch_ms`) and whether it
  finished. Batch them; send on a timer and when the screen goes away. The web
  client's `src/lib/impressions.ts` and the iOS `ImpressionTracker` are
  reference implementations.
- `surface` says where the post was seen (`for_you`, `following`, `discover`,
  `profile`), which is what lets `feed_report` measure the For You page
  separately. The reason a For You post was shown for is recorded by the
  server; the client does not send it.
- Ask for the first page without a cursor on open and on pull to refresh, and
  follow `next` as the reader nears the end of what is loaded.
