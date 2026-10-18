"""Keep the standalone registry transport independent of third-party regex."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "deployment"))
from pull_runtime import bearer_challenge


class RegistryChallengeTests(unittest.TestCase):
    def test_quoted_registry_challenge_with_comma(self):
        self.assertEqual(
            bearer_challenge(
                'Bearer realm="https://registry.example/token",service="registry",scope="repository:a,b:pull"'
            ),
            {"realm": "https://registry.example/token", "service": "registry", "scope": "repository:a,b:pull"},
        )

    def test_other_authentication_schemes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Bearer challenge"):
            bearer_challenge('Basic realm="registry"')


if __name__ == "__main__":
    unittest.main()
