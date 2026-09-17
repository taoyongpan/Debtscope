"""Provider presets and on-disk config round-trip (no network)."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from debtscope.harness.config_store import (  # noqa: E402
    FileConfig, PROVIDERS, load_config_file, masked_key, save_config_file,
)


class PresetsTest(unittest.TestCase):
    def test_preset_count_and_default(self):
        self.assertGreaterEqual(len(PROVIDERS), 16)
        self.assertEqual(next(iter(PROVIDERS)), "doubao",
                         "豆包必须是排序第一的默认预设")
        self.assertEqual(PROVIDERS["doubao"]["model"], "doubao-seed-evolving")

    def test_preset_shape(self):
        for key, p in PROVIDERS.items():
            self.assertIn("label", p)
            self.assertIn("key_url", p)
            if key not in ("custom",):
                self.assertTrue(p["api_base"], f"{key} needs an api_base")
                self.assertTrue(p["api_base"].startswith("http"))
            if key not in ("custom", "ollama"):
                self.assertTrue(p["model"], f"{key} needs a default model")

    def test_ollama_and_custom_present(self):
        self.assertIn("ollama", PROVIDERS)
        self.assertIn("custom", PROVIDERS)
        self.assertEqual(PROVIDERS["ollama"]["key_url"], "")

    def test_config_roundtrip_and_masking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            cfg = FileConfig(provider="deepseek", api_base="https://x/v1/",
                             api_key="sk-secret-123456", model="m")
            save_config_file(cfg, path)
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600, "config file must be owner-only")
            loaded = load_config_file(path)
            self.assertEqual(loaded.provider, "deepseek")
            self.assertEqual(loaded.api_base, "https://x/v1")  # trailing slash trimmed
            self.assertEqual(loaded.api_key, "sk-secret-123456")
            self.assertEqual(loaded.model, "m")

    def test_masked_key(self):
        self.assertEqual(masked_key(None), "")
        self.assertEqual(masked_key("short"), "****")
        self.assertTrue(masked_key("sk-abcdefgh1234").startswith("sk-"))
        self.assertNotIn("secret", masked_key("sk-secret-abcdef"))

    def test_missing_file_is_custom_default(self):
        fc = load_config_file("/nonexistent/config.json")
        self.assertEqual(fc.provider, "custom")


if __name__ == "__main__":
    unittest.main()
