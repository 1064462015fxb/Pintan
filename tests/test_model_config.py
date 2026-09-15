"""模型路由离线测试：只用占位凭证，不联网、不使用真实密钥。"""

import os
import unittest
from unittest.mock import patch

from pintan.model_config import TOKEN_PLAN_URL, load_model_config


class ModelConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {"QWEN_API_KEY": "test-placeholder"}, clear=True)
    def test_roles_and_endpoint(self):
        stable = load_model_config("stable")
        trending = load_model_config("trending")
        self.assertEqual(stable.base_url, TOKEN_PLAN_URL)
        self.assertEqual(stable.model, "qwen3.6-flash")
        self.assertEqual(trending.model, "qwen3.7-plus")
        self.assertNotIn("test-placeholder", repr(stable))

    @patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-placeholder"}, clear=True)
    def test_never_implicitly_send_other_provider_key(self):
        with self.assertRaisesRegex(RuntimeError, "QWEN_API_KEY"):
            load_model_config()

    @patch.dict(os.environ, {"PINTAN_MODEL_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "test-placeholder"}, clear=True)
    def test_explicit_legacy_provider(self):
        self.assertEqual(load_model_config().base_url, "https://api.deepseek.com")

    @patch.dict(os.environ, {"QWEN_API_KEY": "test-placeholder", "QWEN_BASE_URL": "http://example.com"}, clear=True)
    def test_reject_insecure_endpoint(self):
        with self.assertRaises(ValueError):
            load_model_config()

    @patch.dict(os.environ, {"QWEN_API_KEY": "test-placeholder", "PINTAN_TRENDING_MODEL": "qwen3.8-max"}, clear=True)
    def test_role_override(self):
        self.assertEqual(load_model_config("trending").model, "qwen3.8-max")


if __name__ == "__main__":
    unittest.main()
