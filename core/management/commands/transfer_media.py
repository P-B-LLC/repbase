"""Move or copy uploaded photos between storages, and say what does not match.

Media is the one thing here that a deployment cannot recreate. The database
can be migrated and the code redeployed; a lost photo is lost. So the move to
whatever storage outlives the machine -- a mounted volume today, object
storage when there is a bucket -- has to be a copy that can be checked, not a
recursive filesystem copy and a hope.

Three things this does that `cp -r` does not:

- It works from the database, not the directory. The files worth moving are
  the ones a row points at; anything else in the directory is a leftover, and
  copying it forward preserves the leftover instead of the photo.
- It writes through the destination's reserved-name contract, so a file
  arrives under exactly the name its row refers to or the command fails. A
  storage that renames on collision would otherwise quietly produce media
  nothing can find.
- It reports both kinds of mismatch. A referenced file that is not in storage
  is a broken photo; a stored file nothing references is an orphan, which is
  what a failed upload leaves behind when cleanup could not finish.

Backing up is the same operation with a directory as the destination, which
is why there is no separate backup command: a backup nobody can restore is
not a backup, and restoring is this command pointed the other way.
"""

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage, storages
from django.core.management.base import BaseCommand, CommandError

from core.media_storage import ReservedNameFileSystemStorage
from core.models import Post, RepbaseUser


def referenced_names():
    """Every media name a surviving row points at.

    Each field is read the same way on purpose. An earlier version excluded
    only the empty string for profile photos, which let a NULL through and
    added the literal name "None" to the set -- so the audit reported a
    missing file that had never existed. Blank and null are both "no photo".
    """
    names = set()
    for value in RepbaseUser.objects.values_list('profile_photo', flat=True):
        if value:
            names.add(str(value))
    for image, feed in Post.objects.values_list('image', 'feed_image'):
        for value in (image, feed):
            if value:
                names.add(str(value))
    return names

def stored_names(storage, prefix=''):
    """Every file the storage holds, walked depth-first."""
    found = set()
    try:
        directories, files = storage.listdir(prefix)
    except (NotImplementedError, FileNotFoundError, OSError):
        return found
    for name in files:
        found.add(f'{prefix}{name}' if prefix else name)
    for directory in directories:
        found |= stored_names(storage, f'{prefix}{directory}/' if prefix else f'{directory}/')
    return found


class Command(BaseCommand):
    help = 'Copy referenced media to another storage, and report what does not match.'

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group()
        target.add_argument('--to-path', help='Destination directory, for a backup or a volume.')
        target.add_argument('--to-alias', help='Destination STORAGES alias, for object storage.')
        parser.add_argument('--dry-run', action='store_true', help='Report without writing.')
        parser.add_argument(
            '--audit',
            action='store_true',
            help='Only compare the database against the source storage.',
        )

    def destination(self, options):
        if options['to_path']:
            return ReservedNameFileSystemStorage(location=options['to_path'])
        if options['to_alias']:
            try:
                target = storages[options['to_alias']]
            except Exception as error:
                raise CommandError(f'No storage alias {options["to_alias"]!r}.') from error
            # Checked here rather than discovered on the first file. This is
            # the flag the eventual object-storage move uses, and
            # django-storages does not implement the contract -- so without
            # this the move fails partway through with an AttributeError,
            # having already written files under names it chose itself.
            if not callable(getattr(target, 'save_reserved', None)):
                raise CommandError(
                    f'Storage alias {options["to_alias"]!r} does not preserve reserved '
                    'names. Media must arrive under exactly the name its row refers '
                    'to; see the contract in core/media_storage.py.'
                )
            return target
        return None

    def handle(self, *args, **options):
        source = default_storage
        referenced = referenced_names()
        held = stored_names(source)

        missing = sorted(name for name in referenced if not source.exists(name))
        orphans = sorted(held - referenced)

        self.stdout.write(f'referenced by a row: {len(referenced)}')
        self.stdout.write(f'present in storage:  {len(held)}')
        for name in missing:
            self.stdout.write(self.style.ERROR(f'  MISSING  {name}'))
        for name in orphans:
            self.stdout.write(self.style.WARNING(f'  ORPHAN   {name}'))

        if options['audit'] or (target := self.destination(options)) is None:
            if missing:
                raise CommandError(f'{len(missing)} referenced file(s) are not in storage.')
            self.stdout.write(self.style.SUCCESS('Every referenced photo is present.'))
            return

        if missing:
            raise CommandError(
                f'{len(missing)} referenced file(s) are not in storage. '
                'Copying now would carry the gap forward; investigate first.'
            )

        copied = skipped = 0
        for name in sorted(referenced):
            with source.open(name, 'rb') as handle:
                data = handle.read()
            if target.exists(name):
                # Idempotent, so an interrupted run is resumed by running it
                # again rather than by starting over.
                if target.size(name) == len(data):
                    skipped += 1
                    continue
                raise CommandError(f'{name} exists at the destination with a different size.')
            if options['dry_run']:
                copied += 1
                continue
            written = target.save_reserved(name, ContentFile(data))
            if written != name:
                raise CommandError(f'Destination renamed {name} to {written}.')
            if target.size(name) != len(data):
                raise CommandError(f'{name} arrived at a different size.')
            copied += 1

        verb = 'would copy' if options['dry_run'] else 'copied'
        self.stdout.write(f'{verb} {copied}, already present {skipped}')
        self.stdout.write(self.style.SUCCESS('Media transfer complete.'))
