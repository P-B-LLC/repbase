"""Make the card-sized copy for posts that were made before it existed.

New posts get one on upload. This is for the ones already on disk, which
would otherwise go on serving their originals to the feed forever -- correct,
because readers fall back, but the whole point of the variant is that they
should not have to.

Safe to run more than once: posts that already have a variant are skipped
unless --force is given, and a post whose photo cannot be read is reported
and left alone rather than stopping the run.

    python manage.py backfill_feed_photos
    python manage.py backfill_feed_photos --dry-run
"""

from __future__ import annotations

import uuid

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from core.models import Post
from core.photos import feed_variant


class Command(BaseCommand):
    help = "Generate the feed-sized copy of post photos that lack one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be made, and write nothing.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Rebuild variants that already exist, replacing them.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        force = options["force"]

        posts = Post.objects.exclude(image="").exclude(image=None).order_by("pk")
        made = skipped = unreadable = missing = 0
        saved_bytes = 0

        for post in posts:
            if post.feed_image and not force:
                skipped += 1
                continue

            try:
                with post.image.open("rb") as handle:
                    original = handle.read()
            except (FileNotFoundError, OSError):
                # A row pointing at a file that is not there. Reported rather
                # than raised: one missing file must not stop the rest.
                missing += 1
                self.stderr.write(f"  post {post.pk}: file missing ({post.image.name})")
                continue

            smaller = feed_variant(original)
            if smaller is None:
                # Already small enough, or unreadable. Both mean the original
                # is what the feed should serve, which is what happens.
                unreadable += 1
                self.stdout.write(
                    f"  post {post.pk}: no variant worth keeping "
                    f"({len(original):,} bytes)"
                )
                continue

            saved_bytes += len(original) - len(smaller)
            made += 1
            self.stdout.write(
                f"  post {post.pk}: {len(original):,} -> {len(smaller):,} bytes"
            )
            if not dry_run:
                post.feed_image.save(
                    f"{uuid.uuid4().hex}.jpg",
                    ContentFile(smaller),
                    save=True,
                )

        verb = "would make" if dry_run else "made"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{verb} {made} variant(s); {skipped} already had one; "
                f"{unreadable} not worth making; {missing} file(s) missing. "
                f"The feed sends {saved_bytes:,} fewer bytes for one pass "
                f"over these posts."
            )
        )
