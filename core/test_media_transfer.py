"""Moving photos to storage that outlives the machine.

Media is the one thing a deployment cannot recreate, so the move has to be a
copy that can be checked. These cover the three ways a recursive filesystem
copy gets it wrong: carrying leftovers forward, losing the exact name a row
refers to, and reporting success while a referenced file is missing.
"""

import tempfile
from io import StringIO

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from .management.commands.transfer_media import stored_names
from .media_storage import ReservedNameFileSystemStorage
from .models import Post
from .tests import RepbaseAPITestMixin

MEMORY = {'default': {'BACKEND': 'core.media_storage.ReservedNameInMemoryStorage'}}


@override_settings(STORAGES=MEMORY)
class TransferringMediaTests(RepbaseAPITestMixin, APITestCase):
    def setUp(self):
        # InMemoryStorage lives as long as the settings override, which is
        # the whole class, so each test starts by clearing what the last one
        # left rather than colliding with its own reserved names.
        for name in stored_names(default_storage):
            default_storage.delete(name)
        _, self.profile, _ = self.create_account('mover')
        self.post = Post.objects.create(author=self.profile, kind=Post.Kind.MEAL)
        self.post.image = default_storage.save(
            'post-photos/original.jpg', ContentFile(b'original-bytes')
        )
        self.post.feed_image = default_storage.save(
            'post-photos/feed/small.jpg', ContentFile(b'feed')
        )
        self.post.save(update_fields=['image', 'feed_image'])

    def run_command(self, *args):
        out = StringIO()
        call_command('transfer_media', *args, stdout=out)
        return out.getvalue()

    def test_it_copies_what_rows_point_at_and_leaves_the_rest(self):
        """The leftover is the point: a failed upload whose cleanup never
        finished is in the directory and in no row, and copying it forward
        would preserve the litter rather than the photograph."""
        default_storage.save('post-photos/orphan.jpg', ContentFile(b'nobody'))

        with tempfile.TemporaryDirectory() as location:
            output = self.run_command('--to-path', location)
            target = ReservedNameFileSystemStorage(location=location)

            self.assertTrue(target.exists('post-photos/original.jpg'))
            self.assertTrue(target.exists('post-photos/feed/small.jpg'))
            self.assertFalse(target.exists('post-photos/orphan.jpg'))

        self.assertIn('ORPHAN', output)
        self.assertIn('copied 2', output)

    def test_names_arrive_exactly_or_the_command_fails(self):
        """A row stores the name. A destination that renames produces media
        nothing can find, which is silent until somebody opens the app."""
        with tempfile.TemporaryDirectory() as location:
            target = ReservedNameFileSystemStorage(location=location)
            target.save_reserved('post-photos/original.jpg', ContentFile(b'different length'))
            with self.assertRaises(CommandError):
                self.run_command('--to-path', location)

    def test_running_it_twice_copies_nothing_the_second_time(self):
        """So an interrupted transfer is resumed by running it again."""
        with tempfile.TemporaryDirectory() as location:
            self.run_command('--to-path', location)
            again = self.run_command('--to-path', location)
            self.assertIn('copied 0, already present 2', again)

    def test_a_referenced_file_that_is_gone_stops_the_transfer(self):
        """Copying anyway would carry the gap into the new storage and make
        the old one, which still has the evidence, the thing you deleted."""
        default_storage.delete('post-photos/original.jpg')
        with tempfile.TemporaryDirectory() as location:
            with self.assertRaises(CommandError):
                self.run_command('--to-path', location)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as location:
            output = self.run_command('--to-path', location, '--dry-run')
            self.assertIn('would copy 2', output)
            self.assertEqual(ReservedNameFileSystemStorage(location=location).listdir(''), ([], []))

    def test_audit_reports_without_a_destination(self):
        output = self.run_command('--audit')
        self.assertIn('referenced by a row: 2', output)
        self.assertIn('Every referenced photo is present', output)

    def test_audit_fails_when_a_photo_is_missing(self):
        default_storage.delete('post-photos/feed/small.jpg')
        with self.assertRaises(CommandError):
            self.run_command('--audit')


class BackupIsTheSameOperationTests(TestCase):
    """There is no separate backup command because a backup nobody can
    restore is not a backup. Restoring is the transfer pointed the other way,
    so the test is a round trip."""

    @override_settings(STORAGES=MEMORY)
    def test_a_backup_restores(self):
        from .models import RepbaseUser
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username='keeper', password='x' * 12)
        profile = RepbaseUser.objects.create(user=user)
        profile.profile_photo = default_storage.save(
            'profile-photos/face.jpg', ContentFile(b'a face')
        )
        profile.save(update_fields=['profile_photo'])

        with tempfile.TemporaryDirectory() as backup:
            call_command('transfer_media', '--to-path', backup, stdout=StringIO())

            # Lose the live copy the way a machine going away loses it.
            default_storage.delete('profile-photos/face.jpg')
            self.assertFalse(default_storage.exists('profile-photos/face.jpg'))

            restored = ReservedNameFileSystemStorage(location=backup)
            with restored.open('profile-photos/face.jpg', 'rb') as handle:
                default_storage.save('profile-photos/face.jpg', ContentFile(handle.read()))

        with default_storage.open('profile-photos/face.jpg', 'rb') as handle:
            self.assertEqual(handle.read(), b'a face')


@override_settings(STORAGES=MEMORY)
class AnAccountWithNoPhotoTests(RepbaseAPITestMixin, APITestCase):
    """A null profile photo is not a file called "None".

    The first real run of the audit reported one missing file, and the file
    it named was the string "None": the profile query excluded the empty
    string but not NULL, so an account that had never uploaded anything
    produced a reference to a photo that had never existed. Reporting a
    missing photo for every account without one would have made the audit
    useless exactly when it mattered.
    """

    def setUp(self):
        for name in stored_names(default_storage):
            default_storage.delete(name)

    def test_accounts_without_a_photo_reference_nothing(self):
        self.create_account('never-uploaded')
        from .management.commands.transfer_media import referenced_names

        self.assertEqual(referenced_names(), set())

        out = StringIO()
        call_command('transfer_media', '--audit', stdout=out)
        self.assertIn('referenced by a row: 0', out.getvalue())
        self.assertNotIn('None', out.getvalue())
