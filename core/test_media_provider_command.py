from io import StringIO
from unittest.mock import patch
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings
from .openai_media_moderation import MediaVerdict, MediaScreeningUnavailable


@override_settings(MODERATION_API_KEY='test-only', MODERATION_DISCLOSURE_CONFIRMED=True,
                   MEDIA_SCREENING_THRESHOLD=0.5)
class ProviderCommandTests(SimpleTestCase):
    @patch('core.management.commands.check_media_provider.screen_image')
    def test_no_transfer_without_explicit_confirmation(self, screen):
        with self.assertRaises(CommandError):
            call_command('check_media_provider')
        screen.assert_not_called()

    @patch('core.management.commands.check_media_provider.screen_image')
    @override_settings(MODERATION_API_KEY='')
    def test_missing_key_never_calls_provider(self, screen):
        with self.assertRaises(CommandError):
            call_command('check_media_provider', confirm_external_transfer=True)
        screen.assert_not_called()

    @patch('core.management.commands.check_media_provider.screen_image',
           return_value=MediaVerdict(False, 0.01, False, 'test-model'))
    def test_generated_fixture_only(self, screen):
        output = StringIO()
        call_command('check_media_provider', confirm_external_transfer=True, stdout=output)
        self.assertTrue(screen.call_args.args[0].startswith(b'\xff\xd8'))
        self.assertEqual(screen.call_count, 1)
        self.assertIn('does not establish moderation accuracy', output.getvalue())

    @patch('core.management.commands.check_media_provider.screen_image',
           side_effect=MediaScreeningUnavailable())
    def test_provider_failure_is_failure(self, screen):
        with self.assertRaises(CommandError):
            call_command('check_media_provider', confirm_external_transfer=True)
