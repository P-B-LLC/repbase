# Repbase User Guide

## Welcome to Repbase

Repbase helps you organize workouts, record what you accomplish, and follow your progress over time.

The basic workflow is:

> Create exercises → build a workout → start a session → record your sets → finish the session → view your progress

## About the current version

The Repbase mobile interface is being developed separately. The current version is the working service behind the app and can be explored through an interactive web page.

To open the current prototype:

1. Make sure the Repbase server is running.
2. Visit [http://localhost:5000/api/docs/](http://localhost:5000/api/docs/).
3. Use the sections on that page to create and review your fitness data.

The interactive page uses the following controls:

- Select a section to expand it.
- Select **Try it out** to enter information.
- Select **Execute** to submit it.
- Look under **Response body** to see the result.

## 1. Create your account

Open the `POST /api/v1/auth/register/` section and provide:

- A unique username
- Your email address
- A secure password
- Your first and last name

After registration, Repbase returns an access token. This token identifies you when you use the service.

### Sign in later

Use `POST /api/v1/auth/login/` with your username and password. A successful login returns your access token.

## 2. Connect your account to the page

Before working with your private fitness information:

1. Copy the token returned during registration or login.
2. Select **Authorize** near the top of the interactive page.
3. Enter `Token`, followed by a space and your token.

For example:

```text
Token 1234567890abcdef
```

4. Select **Authorize**, then close the window.

Do not share your token. It provides access to your Repbase account.

## 3. Complete your profile

Use `GET /api/v1/me/` to view your profile and `PATCH /api/v1/me/` to update it.

Your profile can include:

- Name and username
- Email address
- Birthdate
- Height
- Current and target weight
- Metric or imperial display preference
- Training style
- Gym
- Profile-photo link
- Body-measurement privacy preference

Repbase stores weight in kilograms. The future iOS interface can display it in the unit you select.

## 4. Create your exercises

Open `POST /api/v1/exercises/` to add an exercise. Enter its name and, if helpful, a muscle group.

Examples include:

- Back Squat — Legs
- Bench Press — Chest
- Barbell Row — Back
- Overhead Press — Shoulders

You can review available exercises through `GET /api/v1/exercises/`.

## 5. Build a workout

### Create the workout

Use `POST /api/v1/workouts/` to give the workout a name and description, such as “Leg Day” or “Upper Body.”

### Add exercises

Use `POST /api/v1/workout-exercises/` for each exercise you want in the workout.

For each entry, choose:

- The workout
- The exercise
- Its position in the workout
- Target number of sets
- Target repetitions
- Optional target weight
- Optional notes

The workout is a reusable plan. Completing a workout session does not overwrite the plan.

## 6. Schedule a workout

Use `POST /api/v1/schedules/` to assign a workout to a date. You can include a short note about the day’s goal.

Use `GET /api/v1/schedules/` to review your schedule. To look at one date, provide it as the `scheduled_date` filter.

## 7. Start a workout session

Use `POST /api/v1/sessions/` and select the workout you plan to perform.

Repbase creates a planned session and copies the workout’s exercises into it. This lets you record actual results without changing the original workout plan.

When you are ready to begin, use:

```text
POST /api/v1/sessions/{session_id}/start/
```

Replace `{session_id}` with the number of your session. The session will change from **planned** to **active**, and Repbase will record the starting time.

## 8. Record your sets

First, use `GET /api/v1/session-exercises/` to find the session exercise you are performing.

Use `POST /api/v1/set-entries/` after completing a set. Record:

- The session exercise
- Set number
- Weight used
- Repetitions completed
- Completion time

Create a separate entry for each set. For example, a three-set squat exercise should have set numbers 1, 2, and 3.

## 9. Finish your workout

When your workout is complete, use:

```text
POST /api/v1/sessions/{session_id}/end/
```

The session changes from **active** to **completed**. Repbase records the ending time and calculates the total duration in seconds.

A session must follow this order:

1. Planned
2. Active
3. Completed

## 10. Follow your progress

Use `GET /api/v1/progress/exercises/{exercise_id}/` to review completed sets for an exercise.

Progress information includes:

- Completion date and time
- Weight used
- Repetitions completed
- Total training volume for the set

Training volume is calculated by multiplying weight by repetitions.

## 11. Track body weight

Use `POST /api/v1/body-weight/` to record a measurement. Include:

- Your weight in kilograms
- The date and time
- An optional note

Use `GET /api/v1/body-weight/` to review your measurement history. Recording separate entries allows Repbase to build a progress graph instead of replacing older measurements.

## Privacy and account security

- Fitness records are limited to the account that created them.
- Other signed-in users only receive limited public profile information.
- Email, birthdate, weight, and other private profile details are not included in the public member list.
- Keep your password and access token private.
- Use Repbase only over HTTPS outside local development.
- Use `POST /api/v1/auth/logout/` when you want to invalidate your current token.

## Common problems

### “Authentication credentials were not provided”

Sign in again, copy your token, and use the **Authorize** button. Be sure to include `Token ` before the token value.

### “Not found” for one of your records

Confirm that you are signed into the account that created the record and that you entered the correct ID.

### A session will not start

Only a planned session can start. Create a new session if the existing session is already active or completed.

### A session will not end

Only an active session can end. Start the session first.

### An exercise does not appear

The exercise may belong to another account. Create your own version or select an available shared exercise.

## Quick reference

| Goal | Section to use |
|---|---|
| Register | `POST /api/v1/auth/register/` |
| Sign in | `POST /api/v1/auth/login/` |
| View or edit profile | `GET/PATCH /api/v1/me/` |
| Create an exercise | `POST /api/v1/exercises/` |
| Create a workout | `POST /api/v1/workouts/` |
| Add an exercise to a workout | `POST /api/v1/workout-exercises/` |
| Schedule a workout | `POST /api/v1/schedules/` |
| Create a session | `POST /api/v1/sessions/` |
| Start a session | `POST /api/v1/sessions/{id}/start/` |
| Record a set | `POST /api/v1/set-entries/` |
| End a session | `POST /api/v1/sessions/{id}/end/` |
| View exercise progress | `GET /api/v1/progress/exercises/{id}/` |
| Record body weight | `POST /api/v1/body-weight/` |

## Additional help

For technical request and response details, see the [Repbase API Guide](API.md). Developers integrating the iOS application should use the validated [OpenAPI specification](openapi.yaml).
