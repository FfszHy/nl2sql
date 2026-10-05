import unittest
from unittest.mock import patch

from app.core.errors import AppError
from app.services import datasource_registry


class CredentialConfigurationTests(unittest.TestCase):
    def test_missing_or_template_secret_cannot_store_credentials(self):
        for secret in (None, "", "replace-with-a-random-local-secret"):
            with self.subTest(secret=secret):
                with patch.object(datasource_registry, "_SECRET", secret):
                    with self.assertRaises(AppError) as caught:
                        datasource_registry._encrypt_password("test-only-placeholder")
                self.assertEqual(caught.exception.code, 1013)
                self.assertEqual(caught.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
