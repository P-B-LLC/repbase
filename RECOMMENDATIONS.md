# The For You page

How recommendations are made, how they keep up with the reader, how to tune
and test changes to them, what the reader controls, what is kept about them,
and what a client has to do. The ranking is `core/recommendations.py`, what
engagement it trusts and what it will not recommend is
`core/feed_integrity.py`, the people suggested beside it are
`core/suggestions.py`, and the endpoints are in `core/for_you.py`.

## Design in one paragraph

Rules, not a model. There is not yet enough interaction data to train anything
trustworthy, and a ranking nobody can read cannot be debugged or explained to
a user. So each reader gets an *interest profile* built from what they did and
said, with what they did in the last few hours kept apart as *momentum*; each
eligible recent post gets a *score* made of named parts with configurable
weights; and the scored posts are *re-ranked* so no creator or topic takes the
page over, people the reader already follows take at most half of it, and one
slot in five goes to something new. Each later page is re-scored against a
fresh profile, so the page keeps up with the reader while they scroll. Every
post on the page says why it is there, and the reader can see what the page has
picked up about them, hide topics, start fresh, or turn personalisation off.

The shape follows what Instagram has published about its own ranking (see
"Where this comes from" below): a post's value is the actions it earns *per
view*, saves and shares first, minus how often people turned it down; only
engagement from accounts that can be trusted counts; and integrity rules --
who and what may be recommended at all -- sit above every ranking preference.

## Signals

What one of the reader's own actions says about what they like:

| Signal | Source | Weight (default) |
| --- | --- | --- |
| Like | `PostLike` | +1.0 |
| Comment | `PostComment` | +2.0 |
| Save (workout or meal copied) | `WorkoutTemplate.source_post`, `SavedFoodMeal.source_post` | +3.0 |
| Share (repost) | `Post.repost_of` | +2.5 |
| Follow | `Follow` | +3.0 to the author |
| Stayed (average view ≥ 5 s) | `PostView.total_dwell_ms / view_count` | +0.5 |
| Came back and stayed again (≥ 2 views, average ≥ 5 s) | `PostView.view_count` | +1.0 (`rewatch`) |
| Watched at least half of a clip | `PostView.max_watch_ms` against the clip's length | +0.5 |
| Watched a clip to the end | `PostView.completed` | +1.0 |
| Scrolled past (< 1.5 s, no watching) | `PostView.skipped` | −0.3 |
| Not interested | `PostFeedback` | −4.0 (×0.75 for the author) |
| Named during onboarding, or a profile discipline | `Personalization`, `UserDiscipline` | +2.0 per topic |

Interactions from the last 90 days count, at most 300 of each kind, each
halving in weight every 21 days. A post's **topics** are its kind, its workout
type or planner category, whether it carries a clip, and -- at half weight --
its author's disciplines. A workout's **detail topics** are its three
most-performed exercises (by sets), when the name is in the shared exercise
catalogue or at least three different people posted workouts with it in the
last 30 days -- a known vocabulary, never one person's private name for
something. A signal's weight is spread over the topics of the post it was
about (detail topics at half a share each), and over its author. Totals are
squashed into −1…1 with `tanh`, so a few signals move a topic a lot and many
more move it only a little further, which is how interest actually saturates.

**Watching is only believed of a clip.** An impression that reports watching
on a post with no approved clip has its `watch_ms` and `completed` dropped,
and a reported watch is clamped to the clip's length, before it is stored.

**Fatigue.** Every author whose posts For You put on the reader's screen in
the last 72 hours, and with whom the reader has never engaged (no positive
action, no follow), is noted: the first post is free, each further one
counts, squashed so three more count about three quarters (`fatigue`). Views
on someone's own profile do not count -- that is seeking them out.

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
   failed review, posts with three or more open reports, and -- unless the
   reader follows them -- **posts by anybody a moderator took a post down from,
   or rejected a clip of, in the last 30 days** (see "Integrity" below).
3. **Candidates** (`candidates`): up to six capped sources, merged --
   - **fresh**: the newest 500 eligible posts;
   - **following**: the newest 150 from people followed;
   - **interest**: the newest 150 on the reader's strongest topics (momentum
     included);
   - **detail**: the newest 150 containing the exercises among those topics,
     when there are any (a query of its own: OR'd into the one above it gives
     PostgreSQL a far worse plan);
   - **similar**: up to 150 posts liked by the reader's taste neighbours (below);
   - **trending**: up to 150 of the 300 posts with the most likes this week,
     counted only from active accounts older than two days and never the
     author's own, worked out once for everyone every five minutes.

   **Similar taste.** The reader's latest positive actions (up to 100 posts)
   are matched against everyone else's likes. People sharing at least two of
   them are weighed by cosine similarity -- shared likes over the geometric
   mean of both histories, so somebody who likes everything is not everybody's
   twin -- times how far their likes can be trusted, and the 50 most alike
   count. Their recent likes are the candidates, each scored by the similarity
   of the people who liked it, relative to the best, and scaled down when fewer
   than five neighbours were found. Blocked, suspended and brand-new accounts
   are nobody's neighbour, and an account that exists to like one person's
   posts counts for less (`Trust.as_neighbour`).
4. **Score** (`score`). Three kinds of part: how well the post suits *this*
   reader, how well it does with *everybody* it was shown to, and what keeps
   the page honest and fresh.

   | Part | Meaning | Weight |
   | --- | --- | --- |
   | topic | weighted mean of the reader's affinity for the post's topics | 2.0 |
   | momentum | the same, against the last six hours only | 1.5 |
   | detail | weighted mean of the reader's affinity for the workout's exercises | 0.5 |
   | author | the reader's affinity for the author | 1.5 |
   | follows | the reader follows the author | 1.0 |
   | similar | how strongly people with the reader's taste liked it (0–1) | 1.2 |
   | quality | trusted engagement per view, smoothed toward 0.1 over 20 views, *explored* | 1.0 |
   | popularity | log-scaled trusted engagement | 0.6 |
   | watch | share of views that stayed (or finished the clip), smoothed | 0.6 |
   | skip_rate | −(share of viewers who scrolled straight past, smoothed toward 0.3 over 20 views, minus 0.3) | 1.0 |
   | hide_rate | −(share of viewers who said "not interested", smoothed toward 0.005 over 50 views, minus 0.005; at most 0.15) | 12.0 |
   | recency | halves every 36 hours | 1.2 |
   | newcomer | author joined in the last 60 days, fading as followers arrive (0 at 30) | 0.4 |
   | seen | how much having seen it counts (below) | −3.0 |
   | fatigue | the author keeps being shown to this reader to no effect (above) | −0.4 |
   | reported | open reports below the exclusion threshold | −1.0 |
   | bait | the caption farms likes (×1.0) or sells something (×0.7) | −1.5 |
   | jitter | tiny noise, so ties do not always break the same way | 0.02 |

   **Engagement** for quality and popularity is `1·likes + 1.5·comments +
   3·saves + 3·shares` (`like_value` … `share_value`), each counted from
   trusted accounts only (below). Saves and shares lead because a save is a
   plan to do the thing and a share is a recommendation to somebody else --
   and because both are what Instagram says matters most for reaching people
   who do not follow the author. A post with no views yet gets the priors for
   skip and hide rates, so those two parts are exactly 0 for it: neither
   helped nor hurt until there is a record. Dismissals never count for more
   than the views behind them, and the hide part is capped at −1.8 -- a heavy
   demotion, but less than a strong topic match is worth -- so no crowd can
   bury a post for the people it suits. Reports, not dismissals, are what take
   a post off the page.

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
   posts while anyone else is available; people the reader follows take at
   most five of any ten consecutive slots while anybody else is available
   (`connected_share`, 0.5); and every fifth slot is **discovery**: one of the
   best few posts by an author the reader has never interacted with, on a topic
   outside their top three, never one they have signalled against. For a
   reader who has said nothing yet -- no history, no onboarding answers, no
   follows -- and for a reader who turned personalisation off, the topic cost
   is up to twice as high (`cold_variety`), falling back to normal by 30
   signals: a wider first page is how the page finds out what they like. These
   rules carry across page boundaries (`RankState`).
6. **Explain** (`_reason`), most personal first:

   | Reason | Label |
   | --- | --- |
   | `discovery` | Something new for you |
   | `following` | From someone you follow |
   | `interest` | Because you like *topic* -- or the exercise, when that matched about as well |
   | `similar` | Liked by people with similar taste -- only when at least two of them did |
   | `new_creator` | From a new creator |
   | `popular` | Popular in the community |
   | `fresh` | New in the community |

   `reason` is a plain string on the wire. A client meeting one it does not
   know shows the label.

## Integrity

What may be recommended at all is decided before anything is ranked, and
whose engagement counts is decided before anything is scored. Both live in
`core/feed_integrity.py`. Nothing there is shown to anybody, returned by the
API, or logged with an identity.

**Whose engagement counts** (`account_trust`, and the counts in
`_count_engagement`). Every like, comment, repost and save counts once per
person and post, as much as that person can be trusted. The database counts
distinct people, leaving out the author and suspended accounts; only the
engagement of the few people who count for less than one -- accounts younger
than 30 days, and accounts bursting this hour -- is read row by row, with when
each joined read from the same rows, so an account made a minute ago is known
to be new however recently anything was cached:

| | Counts for |
| --- | --- |
| the post's own author | nothing |
| a suspended account | nothing |
| a comment a moderator hid | nothing (not even as a row) |
| a comment from somebody the author blocked | nothing |
| an account younger than 48 hours | half |
| an account with more than 60 likes in the last hour | 60 ÷ its likes that hour, at least 0.1 |
| an account younger than 30 days, with ≥ 5 likes on one author in 30 days making up more than 60% of all its likes, on that author's posts | falling from 1 at 60% to 0.2 at 100% |

The last is the sockpuppet rule: a real fan likes other people too. Only young
accounts are asked, because an account that has been around a month and likes
one person is a superfan, and a ring that has to age its accounts first is a
far more expensive ring.

The other side matters as much: a crowd must not be able to *bury* a post
either. Views, scroll-pasts and "not interested" count toward a post's rates
only from accounts past their first two days that are not suspended, a skip
or "not interested" from an account younger than a week counts half, and "not
interested" counts only from somebody who was actually shown the post (a
`PostView` of it).

The rows as they stand are counted too, and `trust` blends between the two (1
= trusted, the default; 0 = rows as before), so the likes, comments, reposts
and saves rules can be run as an experiment.

**What is not recommended**, however it is engaged with:

- **Recent moderation.** An author a moderator took a post down from, or
  rejected a clip of, in the last 30 days is not shown to anybody who does not
  follow them (`MODERATION_COOLDOWN_DAYS`). Their followers chose them and
  still see them. The list is read from posts still hidden, clips still
  rejected, and the access portal's audit of hide decisions -- which outlives
  the post, so deleting a post the portal hid does not end the cooldown. Undoing
  a takedown lifts it. The list is worked out once a minute for everyone; the
  post that was taken down leaves every feed at once, as it always has.
- **Bait and pitches.** A caption that asks for likes, follows, shares or tags
  ("like this if…", "tag a friend who…", "f4f", "follow for more") costs the
  full `bait` weight; one that sells ("DM me for coaching plans", "use my
  code", "link in bio", "giveaway", "fat burner", "lose 10 lbs in 7 days")
  costs 0.7 of it. A short list of unambiguous phrasings rather than a
  classifier, so every demotion can be explained; asking a real question
  ("comment your PR below?") is not on it. A demotion, never a removal.
- **Reports**, as before: three open reports from different people take a post
  out until a moderator has looked; fewer push it down.

Safety comes first in the order of operations: eligibility is applied in the
database before candidates are drawn, and every page is checked against the
visibility rules again as it is served.

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
parts are zero, so the page is ordered by quality, popularity and freshness --
now judged per view and from trusted engagement, so "the best of what is new"
cannot be bought -- and the diversity rules and discovery slots still apply,
with topics varied twice as hard (`cold_variety`): a wide page of the best of
what is new. The first like starts making it personal -- on the very next page.

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
queries one ranking makes does not grow with the community (`ScaleTests`
asserts it, for first and later pages and for suggestions), and the candidate
pool is at most 1,250 posts. What is the same for every reader is computed
once and shared through the cache:

- per-post engagement -- each action as rows and as trusted people, views,
  long views, completions, skips and "not interested" -- for 10 minutes;
- each posted workout's exercises for 6 hours (they never change);
- this week's trending posts for 5 minutes;
- follower counts of recently joined authors, for the newcomer part, for 15
  minutes;
- the lists trust is made from -- suspended accounts, accounts that joined in
  the last 30 days, accounts bursting this hour -- for 5 minutes, and how
  single-minded each young liker is for 15;
- authors under a moderation cooldown, for 1 minute;
- the exercise vocabulary, for an hour.

An established account that is not bursting is simply trusted: nothing is
looked up about it. The reader's own actions are always read live, so nothing
they just did waits on a cache.

Measured on PostgreSQL 16 with 20,000 accounts, 90,000 posts (30,000 inside
the window), 960,000 likes, 3.4 million view rows, 420,000 follows and 120,000
comments, over 30 readers, the ranking before `App-Algorithm` against this one
on the same data and the same machine (two vCPUs, the database on the same
box; each run twice, alternately, and averaged):

| | before p50 | before p95 | after p50 | after p95 | queries before → after |
| --- | --- | --- | --- | --- | --- |
| first page, shared counts cached (normal) | 137 ms | 165 ms | 182 ms | 216 ms | 22 → 24 |
| first page, cache empty (after a restart) | 417 ms | 494 ms | 870 ms | 1,017 ms | 30 → 50 |
| later page, re-ranked | 52 ms | 63 ms | 62 ms | 75 ms | 15 → 15 |
| people suggestions | 84 ms | 110 ms | 85 ms | 109 ms | 29 → 30 |

The cost of counting only trusted engagement is paid when per-post counts are
worked out, which with a shared cache is once per post per ten minutes, not
once per reader; the empty-cache case is a restart without Redis. The
remaining warm-path cost is the `detail` source and scoring more parts.

Indexes it relies on: `PostLike(user, -created_at)`, `PostComment(author,
-created_at)`, `PostView(viewer, -last_seen_at)`, `PostView(surface,
first_seen_at)`, `PostFeedback(viewer, -created_at)`, the unique pairs on the
like, view and feedback tables, the foreign-key indexes the counts read, and
`Post(-created_at, -id)`, which the fresh source walks. No migration was
needed. Two shared lists are refreshed by scans worth knowing about: the
moderation cooldown reads hidden posts once a minute (there is no index on
`is_hidden`; a partial index is the fix if posts reach the millions), and the
bursting list reads the last hour of likes every five minutes, found by
walking back the primary key rather than scanning the table. A later page
re-checks up to 400 remaining candidates; that is most of its cost.

When the community outgrows this -- hundreds of thousands of posts a month --
the next step is a table of per-post counts (raw and trusted) kept up to date
by a scheduled job, in place of the shared cache.

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
  - where attention went (`diversity`): the share of For You impressions the
    ten most-shown authors took, the share from people the reader does not
    follow, the share from new creators, and how often a post was put in
    front of the same reader again on a later visit;
  - **retention**: of the people whose first For You view in the window was
    long enough ago to tell, the share who were back the next day and within a
    week (needs `ANALYTICS_ENABLED`, which records daily activity);
  - ranking latency for first and later pages, as histograms with p50/p95
    bounds;
  - per experiment arm, when one is running (`by_variant`).

  Run it before and after changing weights.

## Evaluating a change before it ships

`core/test_feed_evaluation.py` runs the real ranking, impression recording
and paging over a simulated community whose tastes are known: forty creators
across five topics with a hidden quality per post, sixty members whose
engagement follows their tastes, a ring of twenty fresh accounts liking one
promoter's weak posts, a creator whose captions farm likes, a creator a
moderator took a post down from this week, and five new creators nobody has
found. Twenty-four readers -- warm, onboarded only, and blank -- page through
For You, act on it as their taste says, and come back. The clock is frozen
per arm, so the figures are reproducible.

```sh
FEED_EVAL_REPORT=1 python manage.py test core.test_feed_evaluation    # both arms
FEED_EVAL_ABLATION=1 python manage.py test core.test_feed_evaluation  # each new part off in turn
```

Every new part switched off (the weights below, and no moderation cooldown)
against the defaults. "Off" is close to the ranking before `App-Algorithm` but
not identical to it: the rules that are not weights -- the trending and
neighbour rules for new accounts, trusted views, cleaned impressions, the
separate exercise source -- are on in both arms.

| | all new parts off | defaults |
| --- | --- | --- |
| mean utility of what was served (0–1, what readers actually wanted) | 0.505 | 0.515 |
| utility of the first page for readers with no history | 0.303 | 0.362 |
| share of the first page in a reader's favourite topics | 0.600 | 0.604 |
| served from the ring, bait or the moderated author | 9.7% | 2.2% |
| …of those in anybody's top three | 14 | 0 |
| served from the moderated author | 2.4% | 0% |
| served on a topic the reader dislikes | 4.7% | 4.0% |
| served from new creators | 9.3% | 11.4% |
| from people the reader does not follow | 93.3% | 93.3% |
| distinct authors per 30 posts | 22.7 | 22.3 |
| largest single author's share | 9.2% | 9.0% |

What each new part is worth, by switching it off (mean utility; share served
from the ring, bait or the moderated author): trust 0.515 → 0.497 and 2.2% →
3.8%; rejection rates → 0.508 and 3.1%; bait → 0.504 and 5.4%; the new action
values → 0.510 and 2.9%. Two parts measure slightly *negative* here, inside
what this small world can tell apart: `detail` (0.518 without it) and
`fatigue` (0.521 without it). The simulation treats "did not like it" as a coin
that came up tails, which is weaker evidence than a person ignoring somebody
again and again, and it gives a favourite exercise only a small bonus. Both
ship at modest weights -- fatigue exists to stop the same ignored creator
coming back, which this utility does not score -- and both are the first
things to run as an experiment (below). The slight loss of author spread is
the cost of ranking better posts higher; `author_repeat` is the weight to raise
if it matters more.

The test asserts the claims that should hold whatever the weights: the ring
and bait lose at least half and three quarters of their reach, the moderated
author none of strangers' pages, integrity content never reaches anybody's
top three, and utility -- overall and cold-start -- does not fall.

## Tuning

Every weight above is a field of `FeedWeights`. Override any of them in
settings:

```python
FOR_YOU_WEIGHTS = {"recency": 1.5, "discovery_every": 4, "explore": 0.3}
```

An unknown name stops the server starting (system check `core.E009`) rather
than being silently ignored. Change one weight at a time, check it on the
simulated community first, then compare `feed_report` over the same window
length -- or better, run it as an experiment. Signs to watch: a rising skip or
not-interested rate means the page is showing the wrong things; falling
coverage or a rising `top_ten_author_share` means it is concentrating on fewer
creators; a falling engagement rate with stable coverage usually means recency
is outweighing quality. Do not tune for time spent alone: a page can hold
people longer by being worse for them.

Every scored part added in `App-Algorithm` has a weight that turns it off:

```python
FOR_YOU_WEIGHTS = {
    "trust": 0, "skip_rate": 0, "hide_rate": 0, "bait": 0, "detail": 0, "fatigue": 0,
    "rewatch": 0, "connected_share": 1, "cold_variety": 0,
    "like_value": 1, "comment_value": 2, "save_value": 3, "share_value": 2,
}
```

That is most of the ranking before it, but not all: the eligibility and
counting rules are rules, not weights -- the moderation cooldown
(`MODERATION_COOLDOWN_DAYS` in `core/feed_integrity.py`), trending and taste
neighbours leaving out new accounts, rates counted only from established
viewers, watching believed only of real clips, and the separate exercise
source.

The first experiment worth running is the two parts the simulation could not
settle:

```python
FOR_YOU_EXPERIMENT = {
    "name": "detail-fatigue-2026-11",
    "variants": {"control": {}, "without": {"detail": 0, "fatigue": 0}},
}
```

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

## Where this comes from

Instagram does not publish its ranking, but it has said a good deal about its
shape, and this follows the parts that fit a small community with a readable,
rule-based ranker:

- **Value per view, positives minus negatives.** Explore and Feed score a
  candidate as a weighted sum of predicted actions -- like, save, share,
  comment, follow -- minus predicted negative ones such as "see less", with the
  same weights for everybody and personalisation coming through the
  predictions. Here the predictions are smoothed per-view rates, and the reader's
  own affinities.
- **Shares and saves first for people who do not follow you.** Instagram has
  called sends per reach one of its most important signals, with likes per
  reach mattering more for followers; saves are a named Explore prediction.
- **Skips and "not interested" are negative feedback** that lower a post and
  what is like it, not merely the absence of a like.
- **Integrity above ranking.** Recommendation Guidelines keep some allowed
  content out of recommendations (engagement bait, low-quality and unoriginal
  posts, health and finance claims), accounts that broke the rules lately are
  not recommended for a time, and inauthentic likes and follows are
  discounted.
- **Diversity and discovery.** No long runs from one author; a large share of
  what is shown comes from accounts the reader does not follow; small and new
  creators get a fair first audience.
- **The reader's controls.** Not interested, hidden topics, a visible list of
  what the page has picked up (like Instagram's "Your Algorithm"), and a reset.

Sources: [Instagram Ranking Explained (2023)](https://about.instagram.com/blog/announcements/instagram-ranking-explained),
[Scaling the Instagram Explore recommendations system (2023)](https://engineering.fb.com/2023/08/09/ml-applications/scaling-instagram-explore-recommendations-system/),
[Powered by AI: Instagram's Explore recommender system (2019)](https://ai.meta.com/blog/powered-by-ai-instagrams-explore-recommender-system/),
[Instagram Feed ranking system card (2022)](https://ai.meta.com/tools/system-cards/instagram-feed-ranking/),
[Instagram Recommendation Guidelines](https://help.instagram.com/313829416281232),
[Reducing inauthentic activity on Instagram (2018)](https://about.instagram.com/blog/announcements/reducing-inauthentic-activity-on-instagram/),
[A new ranking framework for notification quality on Instagram (2025)](https://engineering.fb.com/2025/09/02/ml-applications/a-new-ranking-framework-for-better-notification-quality-on-instagram/),
[Reshape your Instagram with a recommendations reset (2024)](https://about.fb.com/news/2024/11/introducing-recommendations-reset-instagram/),
[Instagram shares algorithm insights (Social Media Today, 2025)](https://www.socialmediatoday.com/news/instagram-shares-algorithm-insights-2025/738034/).

## Known limits

- **Not every takedown outlives its post.** A post hidden in the Django admin,
  or a clip rejected there, leaves no record that survives the post being
  deleted, so an author who deletes it ends their cooldown early. The access
  portal's decisions are audited and do not have this gap. The fix is for every
  moderation path to write the same audit row (or a `moderated_at` on the
  account); it touches the moderation code, so it is proposed separately. The
  reverse also happens: a portal hide that was undone in the admin, on a post
  its author then deleted, looks like a hide nobody undid, and puts the author
  back under the cooldown until 30 days after the decision. Recording undos in
  the same audit fixes both.
- **Reports are counted from anybody.** Three open reports from different
  accounts -- fresh ones included -- take a post out of For You until a
  moderator looks. That predates this branch and is deliberately safety-first;
  weighting reports by trust would trade some of that safety for resistance to
  report-bombing, which is a moderation decision.
- **Suspensions reach the counts within five minutes** (the shared list of
  suspended accounts), and moderation decisions the cooldown within a minute.
  The suspended list only grows; at thousands of entries it is still a hashed
  lookup, but it is the thing to move into a join if it reaches tens of
  thousands.
- **Young accounts are read row by row.** Engagement from accounts younger
  than 30 days is fetched per person to weigh it; while most of a community is
  that young -- at launch -- that is most of its engagement. It is cached per
  post like everything else; `FOCUS_YOUNG_DAYS` is the dial.
- **Exercise names come from posts.** A name three different people used in
  the last month is a topic, and can appear in a reader's own interests and
  reasons. Only the reader ever sees their interests.
- **The simulation is a small world.** Differences in utility under about
  0.01 are within what it can tell apart; it is for catching regressions and
  manipulation, not for fine-tuning weights. `feed_report` and experiments on
  real traffic are.

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
