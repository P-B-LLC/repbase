from django.test import SimpleTestCase
from deploy.configure_video import updated_environment


class VideoConfigurationTests(SimpleTestCase):
    def test_preserves_secrets_and_unrelated_configuration(self):
        original = '# private\nDATABASE_URL="fixture with spaces"\nexport VIDEO_PIPELINE_ENABLED=false\nVIDEO_PIPELINE_ENABLED=false\nSMTP_PASSWORD="not-a-real-secret"\n'
        updated = updated_environment(original, True)
        self.assertIn('DATABASE_URL="fixture with spaces"\n', updated)
        self.assertIn('SMTP_PASSWORD="not-a-real-secret"\n', updated)
        self.assertEqual(updated.count('VIDEO_PIPELINE_ENABLED='), 1)
        self.assertIn('VIDEO_PIPELINE_ENABLED=true\n', updated)
        self.assertEqual(updated_environment(updated, True), updated)

    def test_defaults_to_disabled_without_erasing_private_storage_settings(self):
        updated = updated_environment('OTHER_SETTING=kept\n', False)
        self.assertIn('VIDEO_PIPELINE_ENABLED=false\n', updated)
        self.assertIn('VIDEO_STORAGE_BACKEND=local\n', updated)
        self.assertIn('OTHER_SETTING=kept\n', updated)
