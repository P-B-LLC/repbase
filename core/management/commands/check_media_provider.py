"""Opt-in live connectivity check; never loads uploads or changes publication."""
from io import BytesIO
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw
from core.openai_media_moderation import screen_image, MediaScreeningUnavailable


class Command(BaseCommand):
    help = 'Send one generated geometric image to OpenAI; not an accuracy evaluation.'

    def add_arguments(self, parser):
        parser.add_argument('--confirm-external-transfer', action='store_true')

    def handle(self, *args, **options):
        if not options['confirm_external_transfer']:
            raise CommandError('Requires --confirm-external-transfer; sends one synthetic image to OpenAI.')
        if not settings.MODERATION_API_KEY or not settings.MODERATION_DISCLOSURE_CONFIRMED:
            raise CommandError('Configure the API key and confirmed disclosure securely before this check.')
        image = Image.new('RGB', (256, 256), 'white')
        ImageDraw.Draw(image).rectangle((64, 64, 192, 192), fill='blue')
        buffer = BytesIO()
        image.save(buffer, format='JPEG')
        try:
            verdict = screen_image(buffer.getvalue(), threshold=settings.MEDIA_SCREENING_THRESHOLD,
                                   consent_confirmed=True, child_safety_hold=False)
        except (MediaScreeningUnavailable, ValueError):
            raise CommandError('Provider check failed. No posts were changed; credentials are not logged.') from None
        if verdict.flagged:
            raise CommandError('Provider returned review for the synthetic fixture. Investigate before activation.')
        self.stdout.write(self.style.SUCCESS(
            'Live provider response validated for one synthetic image. No posts changed. '
            'This does not establish moderation accuracy.'))
