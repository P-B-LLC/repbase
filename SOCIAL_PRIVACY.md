# What the social pages use, keep and never touch

This is the description the privacy policy, the App Store privacy answers and
the in-app wording have to match. It describes what the code does, and each
claim names the test that holds it in place (`core/test_social_insights.py`
unless another file is named). If the code changes, change this file in the
same commit.

## What the For You page uses

Only things a person did on the social pages, or told the app about
themselves for this purpose:

| Used | Where it comes from |
| --- | --- |
| Likes, comments, reposts, saves of a post, takes | The reader's own social actions |
| "I tried this" | A button the reader presses. Never inferred (see below) |
| "Not interested", "fewer like this", "more like this" | The reader's own feedback |
| Which posts were on screen, for how long, how much of a clip played | Measured on the device, bounded by the server (`core/attention.py`) |
| Who they follow | Their follows |
| Training types named in onboarding, disciplines on their profile, interests they added | What they declared |

## What it never uses

HealthKit data, workout sessions and sets, routes and location, body weight
and measurements, step counts, meal logs and nutrition goals, planner
entries, contacts, and anything medical. None of these is read by the
ranking, the learned model, the attention pipeline, people suggestions or the
private insights page.

- `PrivateDataBoundaryTests.test_nothing_that_ranks_or_measures_reads_private_records`
  fails if one of those modules so much as names a private model.
- `test_private_training_does_not_move_the_profile` logs workouts and checks
  the reader's profile is unchanged.
- `TriedTests.test_nothing_tracked_ever_says_it_for_you`: saving a workout
  from a post and then doing it does not create an "I tried this".

A post is public content its author chose to publish; a workout or recipe
attached to a post is part of that post. That is different from the private
record it was made from, which stays private.

## What is kept, and for how long

| Record | Kept | Removed by |
| --- | --- | --- |
| Viewing history (`PostView`) | 90 days, or less with `RECOMMENDATION_HISTORY_DAYS` | `prune_expired_rows`, in batches |
| What the ranking knew about a served post (`PostView.features`) | 14 days, optionally a sample (`FOR_YOU_TRAINING_SAMPLE_RATE`) | `prune_expired_rows` |
| "Not interested" and similar feedback | 90 days | `prune_expired_rows` |
| Attention batch receipts | 7 days | `prune_expired_rows` |
| Account-day API presence | 30 days | `prune_analytics` |
| Operational counters (`SocialDailyCounter`) | 90 days | `roll_up_social_insights` |
| Insight snapshots | Not removed: counts and rates about nobody, with small groups never written | — |

`python -m config.deploy maintenance` runs all of these daily.

## Controls a person has

- **Turn personalisation off** (`PATCH social/for-you/preferences/`): the page
  stops using what they do and orders by quality, popularity and freshness.
- **Hide a topic, add an interest** (same endpoint).
- **Start fresh** (`DELETE social/for-you/history/`): deletes the viewing
  history and its training samples at once, and stops earlier actions
  counting. Likes and comments stay; they are the person's own content.
- **Delete the account**: every row above that names the account goes with it
  (`test_deleting_the_account_deletes_what_was_kept_about_it`).

## Logs and staff access

Feed and telemetry logs carry counts, durations and reason names. They carry
no caption, token, email address or media address
(`test_the_feed_and_its_telemetry_log_no_content_or_credentials`).

Staff do not see individual activity. `/insights/social/` is limited to the
analytics role, reads daily snapshots only, and withholds any figure with
fewer than five people behind it (`InsightsPageTests`, `ProductFigureTests`).

## App Store

Apple restricts health data from being used for advertising or data mining
(guideline 5.1.3). The position above is that health and fitness records are
not used for recommendations at all. Before submitting, check that:

1. The privacy answers list "product interaction" and "other user content"
   as used for personalisation, linked to the account, and do **not** list
   health or fitness data under personalisation.
2. The privacy policy describes the viewing history, its 90-day limit, and
   the three controls above.
3. The For You settings screen in the build offers all three controls.

None of the three has been checked against a submitted build. The code can
hold the boundary; it cannot check what the store listing says.
