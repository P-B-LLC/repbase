# Repbase Product Feature Summary

## Product vision

Repbase is a fitness platform designed to help users plan workouts, log training sessions, measure progress, and stay organized. The strongest core experience is:

> Plan a workout → start a session → log sets → end the session → view progress → optionally share it

The broader concept also includes nutrition tracking, personal scheduling, social features, gym communities, and coach-client collaboration.

## Feature areas and deliverables

### 1. Accounts and profiles

Features:

- Sign up with Google or a username and password
- First name, last name, username, birthdate, and profile photo
- Height, current weight, and target weight
- Option to hide body measurements from the public profile
- Training style, such as powerlifting, bodybuilding, or CrossFit
- Gym affiliation

Deliverables:

- Authentication and onboarding
- Editable user profile
- Profile privacy controls
- Profile API

### 2. Workout planning

Features:

- Create reusable workouts
- Add exercises, sets, repetitions, and target weights
- Assign workouts to days of the week
- Display the current day's workout on the home screen

Deliverables:

- Exercise library
- Workout builder
- Weekly workout schedule
- Workout-template API

### 3. Workout sessions

Features:

- Start and end a workout session
- Enter actual weight and repetitions for each set
- Preserve planned values until the session begins
- Calculate plates needed on each side of a barbell
- Invite another user to a shared session
- Save completed session data to the backend

Deliverables:

- Session logger
- Session lifecycle and status handling
- Set-entry interface
- Plate calculator
- Session API

### 4. Progress tracking

Features:

- Exercise progress by weight and repetitions
- Exercise progress over time
- Body-weight history
- Potential metrics such as volume and estimated one-rep max

Deliverables:

- Progress dashboard
- Exercise charts
- Body-weight chart
- Historical-data endpoints

### 5. Home dashboard

Features:

- Today's workout
- Daily calories and macronutrients
- High-priority or missed tasks
- Upcoming events
- Social notifications

Deliverables:

- Consolidated home screen
- Dashboard API

### 6. Nutrition

Features:

- Log foods by breakfast, lunch, dinner, and snacks
- Track calories, protein, and other macronutrients
- Potential vitamin tracking through an external data provider
- Optional AI-powered dietary suggestions

Deliverables:

- Food diary
- Daily nutrition totals
- Meal history
- Nutrition-data integration

### 7. Calendar and tasks

Features:

- Create events and to-do items
- Assign high, medium, or low priority
- Set deadlines, times, and descriptions
- Receive suggested preparation tasks for upcoming events

Deliverables:

- Calendar interface
- Task manager
- Reminders and notifications
- Event and task APIs

### 8. Social and gym communities

Features:

- Share workouts, recipes, photos, events, and general posts
- Like, share, save, and report content
- Create communities based on gym membership
- Share completed sessions or personal records

Deliverables:

- Social feed
- Post composer and media upload
- Social interactions
- Reporting and moderation tools
- Gym directory and community pages

### 9. Coaching

Features:

- Connect a user with a coach profile
- Allow coaches to assign workouts and meal plans
- Track client progress

Deliverables:

- Coach role and permissions
- Coach-client relationships
- Workout and meal-plan assignment workflow
- Client progress dashboard

## Recommended MVP

The first release should focus on the core workout loop:

1. Account creation and basic profiles
2. Exercise library
3. Reusable workout templates
4. Weekly workout scheduling
5. Start and end workout sessions
6. Set, weight, and repetition logging
7. Exercise and body-weight progress charts
8. A simple today's-workout dashboard
9. Django API endpoints for the iOS app

## Suggested roadmap

### Phase 1: Workout MVP

- Profiles
- Workout templates and schedules
- Session logging
- Progress charts
- Basic dashboard

### Phase 2: Community

- Session invitations
- Workout sharing
- Basic social feed
- Gym communities

### Phase 3: Lifestyle organization

- Food and macro tracking
- Calendar and task management
- Notifications
- Event preparation

### Phase 4: Advanced services

- Coach-client relationships
- Assigned workouts and meal plans
- AI suggestions
- Nutrition and vitamin integrations

## Suggested backend entities

The existing `RepbaseUser` and `Session` models are a useful starting point. The workout system will likely need:

- `Exercise`
- `WorkoutTemplate`
- `WorkoutExercise`
- `WorkoutSchedule`
- `Session`
- `SessionExercise`
- `SetEntry`
- `BodyWeightEntry`

A session should represent one performance of a workout. Individual set entries should store the actual weight and repetitions performed.

## Open product decisions

- Is Repbase primarily a workout tracker or a broader life organizer?
- Will measurements support metric, imperial, or both?
- Can users create custom exercises?
- Which profile and workout fields are public?
- Are shared sessions live and collaborative or shared after completion?
- Which progress metrics should be emphasized?
- What permissions should coaches have over client data?
- How should health, nutrition, and AI recommendations be reviewed and limited?

## Assessment

Workout planning, live logging, and progress visualization form the most developed and compelling product concept. Nutrition, scheduling, social networking, and coaching are promising expansion areas, but they should remain separate roadmap tracks so the initial release stays focused and achievable.
