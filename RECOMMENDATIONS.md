# The For You page

How recommendations are made, how to tune them, how to tell whether they are
working, and what a client has to do to use them. The code is
`core/recommendations.py`; the endpoints are in `core/for_you.py`.

## Design in one paragraph

Rules, not a model. There is not yet enough interaction data to train anything
trustworthy, and a ranking nobody can read cannot be debugged or explained to
a user. So each reader gets an *interest profile* built from what they did and
said; each eligible recent post gets a *score* made of named parts with
configurable weights; and the scored posts are *re-ranked* so no creator or
topic takes the page over and one slot in five goes to something new. Every
post on the page says why it is there.

## Signals

| Signal | Source | Weight (default) |
| --- | --- | --- |
| Like | `PostLike` | +1.0 |
| Comment | `PostComment` | +2.0 |
| Save (workout or meal copied) | `WorkoutTemplate.source_post`, `SavedFoodMeal.source_post` | +3.0 |
| Share (repost) | `Post.repost_of` | +2.5 |
| Follow | `Follow` | +3.0 to the author |
| Stayed (average view ≥ 5 s) | `PostView.total_dwell_ms / view_count` | +0.5 |
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

Onboarding answers map to topics: Strength → lifting and the strength
disciplines; Running, Cycling, Swimming → their workout type and discipline
(plus triathlon); Nutrition → meals; Training → workouts.

## Pipeline

1. **Profile** (`build_profile`): the signals above, read with bounded queries.
2. **Eligibility** (`eligible_posts`): built on `visible_posts_for`, so a post
   the reader could not open is never a candidate -- hidden, blocked either
   way, private, followers-only without a follow, closed profile, suspended
   author. On top of that it removes the reader's own posts, reposts (the
   original is the candidate), anything older than 30 days, posts the reader
   reported or dismissed, posts whose clip is waiting for or failed review,
   and posts with three or more open reports.
3. **Candidates** (`candidates`): three capped sources, merged -- the newest
   500 eligible posts, the newest 150 from people followed, and the newest 150
   matching the reader's five strongest topics -- each annotated with like,
   comment, share, save, view, long-view and completion counts in the same
   query.
4. **Score** (`score`):

   | Part | Meaning | Weight |
   | --- | --- | --- |
   | topic | weighted mean of the reader's affinity for the post's topics | 2.0 |
   | author | the reader's affinity for the author | 1.5 |
   | follows | the reader follows the author | 1.0 |
   | quality | engagement per view, smoothed toward a prior of 0.1 over 20 views | 1.0 |
   | popularity | log-scaled engagement (likes + 2·comments + 3·saves + 2·shares) | 0.6 |
   | recency | halves every 36 hours | 1.2 |
   | watch | share of views that stayed (or finished the clip), smoothed | 0.6 |
   | seen | −1 if seen in the last 3 days, −1.5 if skipped | 2.5 |
   | reported | open reports below the exclusion threshold | 1.0 |
   | jitter | tiny noise, so ties do not always break the same way | 0.02 |

5. **Rank** (`rank`): highest adjusted score first, where each earlier
   appearance of the same author costs 1.0 and each recent appearance of the
   same main topic costs 0.35; the same author never appears twice within four
   posts while anyone else is available; and every fifth slot is **discovery**:
   one of the best few posts by an author the reader has never interacted with,
   on a topic outside their top three, never one they have signalled against.
6. **Explain** (`_reason`): `following`, `interest` ("Because you like
   running"), `discovery`, `popular` or `fresh`, sent with each post.

## New accounts

With no history, the onboarding answers and profile disciplines are a prior
that personalises the very first page. With nothing at all, topic and author
parts are zero, so the page is ordered by quality, popularity and freshness,
and the diversity rules and discovery slots still apply: a varied page of the
best of what is new. The first like starts making it personal.

## Paging, caching and freshness

A ranking is not an `ORDER BY`, so it is computed once per first page (up to
200 posts), cached for 30 minutes under the reader's id and a random session
name, and paged by position. The cursor is signed, so it cannot be forged, and
the cache key includes the reader, so a cursor from someone else opens nothing
of theirs. Each page is re-checked against the visibility rules and the
reader's dismissals as it is served, so a post hidden, deleted, blocked or
dismissed after the ranking was made does not appear. Requesting without a
cursor always ranks afresh, which is how the page reflects what the reader just
did. If the cached ranking has expired, the next page ranks again and carries
on from the same position.

## Performance

Every query is bounded by a cap, not by the amount of content: the number of
queries one ranking makes is constant (`ScaleTests` asserts it), and the
candidate pool is at most 800 posts. Indexes added for it: `PostLike(user,
-created_at)`, `PostComment(author, -created_at)`, `PostView(viewer,
-last_seen_at)`, `PostView(surface, first_seen_at)`, `PostFeedback(viewer,
-created_at)`, and the unique pairs on both new tables. The newest-posts source
walks the existing `Post(-created_at, -id)` index.

When the community outgrows this -- tens of thousands of posts a day -- the
next steps are precomputing per-post engagement into a table refreshed by a
scheduled job instead of correlated counts, and caching interest profiles for a
few minutes.

## Monitoring

- **`Server-Timing: rank;dur=…`** on every freshly ranked page, visible in
  any browser's network panel.
- **A log line per ranking** on the `core.recommendations` logger (INFO): how
  many posts were ranked from how many candidates, whether the reader was new,
  the time taken and the mix of reasons. No user identifiers. In production,
  route that logger to your log collector to see it.
- **`python manage.py feed_report [--days 7] [--hours 24] [--json]`**: counts
  and rates only --
  - impressions, viewers and distinct posts and authors shown (coverage);
  - engagement rate: the share of For You views the reader then liked,
    commented on, saved or reposted;
  - skip rate, not-interested rate and clip completion rate;
  - average dwell;
  - ranking latency as a histogram with p50/p95 bounds.

  Run it before and after changing weights.

## Tuning

Every weight above is a field of `FeedWeights`. Override any of them in
settings:

```python
FOR_YOU_WEIGHTS = {"recency": 1.5, "discovery_every": 4}
```

An unknown name is a startup error rather than a silently ignored typo. Change
one weight at a time and compare `feed_report` over the same window length.
Signs to watch: a rising skip or not-interested rate means the page is showing
the wrong things; falling coverage means it is concentrating on fewer
creators; a falling engagement rate with stable coverage usually means
recency is outweighing quality.

## API for clients

```text
GET    /api/v1/social/for-you/?cursor=…&page_size=…   page of posts, each with recommendation {reason, label}
POST   /api/v1/social/impressions/                    {"events": [{post, dwell_ms, watch_ms, completed, surface}]}, ≤ 50
POST   /api/v1/social/posts/{id}/not-interested/      204
DELETE /api/v1/social/posts/{id}/not-interested/      204
POST   /api/v1/social/posts/{id}/video/               raw MP4/QuickTime body; returns the post
DELETE /api/v1/social/posts/{id}/video/               returns the post
```

`Post` and `RepostedPost` gained a nullable `video` object (`url`,
`content_type`, `duration_ms`, `width`, `height`, `has_audio`, `status`). A
clip that is not `approved` is sent only to its author.

### What a client should send

- An impression for each post that was at least 60% on screen, with how long
  (`dwell_ms`), and for a clip how far it got (`watch_ms`) and whether it
  finished. Batch them; send on a timer and when the screen goes away. The web
  client's `src/lib/impressions.ts` is a reference implementation.
- `surface` says where the post was seen (`for_you`, `following`, `discover`,
  `profile`), which is what lets `feed_report` measure the For You page
  separately.

### Adopting it on iOS

The web client uses all of it. The iOS app was not changed, because it could
not be built and tested without Xcode. To adopt it there:

1. Copy `openapi.yaml` to `IOS-Frontend/API/openapi.yaml` and run
   `bash Scripts/generate-api-client.sh`, then build.
2. Add a For You tab reading `socialForYouList`, following `next` until it is
   null, and show `recommendation.label` under each post.
3. Report impressions from the feed's visibility tracking, and offer "Not
   interested" in the post's menu.
4. Play `video.url` with AVPlayer (the server answers byte ranges), and show
   the `pending`/`rejected` status to the author.
5. Upload clips with `URLSession.upload(for:fromFile:)` to the video endpoint,
   with the file's MIME type. Export to H.264 or HEVC, under 60 seconds and
   25 MB, before uploading.
