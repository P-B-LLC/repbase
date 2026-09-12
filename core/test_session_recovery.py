"""A lost reply must not cost the user their session or their set.

Every test here is the same situation from the phone's side: the request
reached the server, the server did the work, and the reply never arrived. The
phone cannot tell that apart from a request that never landed, so it sends it
again -- and what it needs back is what happened the first time, not an error
about the state its own earlier request produced.

Two mechanisms, because the writes are two different shapes.

Session start is a transition into a known state, so it needs no key: a retry
is asking "is it started?" and the honest answer is yes, with the session.
Creates are not -- two identical POSTs are normally two rows -- so those carry
an Idempotency-Key and a receipt, and the second one replays the first's
response rather than logging the set twice.
"""

import uuid

from rest_framework.test import APITestCase

from .models import Exercise, SaveReceipt, SetEntry, SessionExercise, WorkoutSession
from .tests import RepbaseAPITestMixin


class StartingASessionTwiceTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        self.user, self.profile, self.token = self.create_account("lifter")
        self.authenticate(self.token)
        self.session = WorkoutSession.objects.create(repbase_user=self.profile)

    def start(self):
        return self.client.post(f"/api/v1/sessions/{self.session.id}/start/")

    def test_a_retried_start_replays_instead_of_conflicting(self):
        """The bug: the second call used to answer 409 for a session that
        started perfectly well the first time."""
        first = self.start()
        self.assertEqual(first.status_code, 200, first.data)

        second = self.start()
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(second.data["status"], WorkoutSession.Status.ACTIVE)

    def test_a_retried_start_does_not_move_the_clock(self):
        """Otherwise a retry quietly rewrites when the workout began."""
        self.assertEqual(self.start().status_code, 200)
        self.session.refresh_from_db()
        began = self.session.started_at

        self.assertEqual(self.start().status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.started_at, began)

    def test_starting_a_finished_session_is_still_refused(self):
        """Replaying is for the state the caller asked for, not any state.

        Starting a session that is over is a real mistake rather than a lost
        reply, and answering 200 to it would hide one.
        """
        self.session.status = WorkoutSession.Status.COMPLETED
        self.session.save(update_fields=["status"])
        self.assertEqual(self.start().status_code, 409)

    def test_one_account_cannot_start_another_account_s_session(self):
        """The lock is taken on an owner-scoped query, so this is worth
        holding: it is the line the scoping change touched."""
        _, _, other_token = self.create_account("stranger")
        self.authenticate(other_token)
        self.assertEqual(self.start().status_code, 404)


class LoggingASetTwiceTests(RepbaseAPITestMixin, APITestCase):
    """The create half, which needs a key because two POSTs are normally two
    rows and the server cannot tell a retry from a second set of the same
    weight and reps -- which is an ordinary thing to do."""

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("lifter")
        self.authenticate(self.token)
        self.session = WorkoutSession.objects.create(repbase_user=self.profile)
        self.exercise = SessionExercise.objects.create(
            session=self.session,
            exercise=Exercise.objects.create(name="Bench press", created_by=self.profile),
            order=1,
        )
        self.key = str(uuid.uuid4())

    def log(self, key=None, reps=8, set_number=1):
        """A retry sends what it sent before, so the body is fixed by default."""
        return self.client.post(
            "/api/v1/set-entries/",
            {
                "session_exercise": self.exercise.id,
                "set_number": set_number,
                "reps": reps,
                "weight_kg": "60",
            },
            format="json",
            headers={"Idempotency-Key": key} if key else {},
        )

    def test_the_same_key_logs_one_set(self):
        first = self.log(self.key)
        self.assertEqual(first.status_code, 201, first.data)
        second = self.log(self.key)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(SetEntry.objects.filter(session_exercise=self.exercise).count(), 1)

    def test_without_a_key_the_retry_fails_on_the_set_number(self):
        """What the key is actually buying here, which is not de-duplication.

        Set numbers are unique per exercise, so a retried set was never going
        to land twice -- it lands as a 400 about a set number the user never
        typed, for a set the server already has. The key turns that back into
        the 201 the phone missed.
        """
        self.assertEqual(self.log().status_code, 201)
        repeated = self.log()
        self.assertEqual(repeated.status_code, 400, repeated.data)
        self.assertEqual(SetEntry.objects.filter(session_exercise=self.exercise).count(), 1)

    def test_the_same_key_with_different_values_is_refused(self):
        """A retry carries the values it was sent with. Different values under
        the same key is a client bug, and answering the old response would
        hide it."""
        self.assertEqual(self.log(self.key).status_code, 201)
        changed = self.log(self.key, reps=12)
        self.assertEqual(changed.status_code, 409, changed.data)
        self.assertEqual(SetEntry.objects.filter(session_exercise=self.exercise).count(), 1)

    def test_a_receipt_belongs_to_the_account_that_made_it(self):
        self.assertEqual(self.log(self.key).status_code, 201)
        receipt = SaveReceipt.objects.get(key=self.key)
        self.assertEqual(receipt.owner_id, self.profile.id)


class CreatingASessionTwiceTests(RepbaseAPITestMixin, APITestCase):
    """The other half of "create then start": the create."""

    def setUp(self):
        self.user, self.profile, self.token = self.create_account("lifter")
        self.authenticate(self.token)
        self.key = str(uuid.uuid4())

    def create(self, key=None):
        return self.client.post(
            "/api/v1/sessions/", {}, format="json",
            headers={"Idempotency-Key": key} if key else {},
        )

    def test_a_retried_create_makes_one_session(self):
        first = self.create(self.key)
        self.assertEqual(first.status_code, 201, first.data)
        second = self.create(self.key)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(WorkoutSession.objects.filter(repbase_user=self.profile).count(), 1)

    def test_create_then_start_survives_losing_both_replies(self):
        """The whole of the gate item, end to end.

        Both requests land, both replies are lost, the phone sends both again.
        What it must end up with is one session, started once.
        """
        created = self.create(self.key)
        self.assertEqual(created.status_code, 201, created.data)
        session_id = created.data["id"]

        self.assertEqual(self.client.post(f"/api/v1/sessions/{session_id}/start/").status_code, 200)

        # The phone never heard either answer, so it repeats both.
        self.assertEqual(self.create(self.key).data["id"], session_id)
        retried = self.client.post(f"/api/v1/sessions/{session_id}/start/")
        self.assertEqual(retried.status_code, 200, retried.data)

        self.assertEqual(WorkoutSession.objects.filter(repbase_user=self.profile).count(), 1)
        session = WorkoutSession.objects.get(pk=session_id)
        self.assertEqual(session.status, WorkoutSession.Status.ACTIVE)
