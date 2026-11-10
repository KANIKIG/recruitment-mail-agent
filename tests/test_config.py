import os
import unittest
from unittest.mock import patch

from autumn_tracker.config import Settings


class LlmConfigTest(unittest.TestCase):
    def test_generic_llm_variables_take_precedence(self):
        environment = {
            "LLM_API_KEY": "generic-key",
            "OPENAI_API_KEY": "openai-key",
            "DEEPSEEK_API_KEY": "deepseek-key",
            "LLM_BASE_URL": "https://gateway.example.com/v1/",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "LLM_MODEL": "provider-model",
            "LLM_BATCH_SIZE": "5",
            "LLM_JSON_MODE": "false",
            "LLM_SYSTEM_ROLE": "developer",
            "LLM_MAX_TOKENS_PARAM": "max_completion_tokens",
            "LLM_EXTRA_BODY_JSON": '{"temperature":0.1}',
            "LLM_EXTRA_HEADERS_JSON": '{"X-Provider":"example"}',
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = Settings.from_env(require_targets=False, require_mail=False)

        self.assertEqual(settings.llm_api_key, "generic-key")
        self.assertEqual(settings.llm_base_url, "https://gateway.example.com/v1")
        self.assertEqual(settings.llm_endpoint, "https://gateway.example.com/v1/chat/completions")
        self.assertEqual(settings.llm_model, "provider-model")
        self.assertEqual(settings.llm_batch_size, 5)
        self.assertFalse(settings.llm_json_mode)
        self.assertEqual(settings.llm_system_role, "developer")
        self.assertEqual(settings.llm_max_tokens_param, "max_completion_tokens")
        self.assertEqual(settings.llm_extra_body, {"temperature": 0.1})
        self.assertEqual(settings.llm_extra_headers, {"X-Provider": "example"})

    def test_legacy_deepseek_variables_remain_supported(self):
        environment = {
            "LLM_API_KEY": "",
            "OPENAI_API_KEY": "",
            "DEEPSEEK_API_KEY": "legacy-key",
            "LLM_BASE_URL": "",
            "OPENAI_BASE_URL": "",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
            "LLM_MODEL": "",
            "OPENAI_MODEL": "",
            "DEEPSEEK_MODEL": "deepseek-v4-flash",
            "LLM_EXTRA_BODY_JSON": "",
            "LLM_EXTRA_HEADERS_JSON": "",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = Settings.from_env(require_targets=False, require_mail=False)

        self.assertEqual(settings.llm_api_key, "legacy-key")
        self.assertEqual(settings.llm_model, "deepseek-v4-flash")
        self.assertEqual(settings.llm_endpoint, "https://api.deepseek.com/chat/completions")
        self.assertEqual(settings.llm_extra_body, {"thinking": {"type": "disabled"}})

    def test_full_chat_completions_url_can_be_overridden(self):
        with patch.dict(os.environ, {
            "LLM_CHAT_COMPLETIONS_URL": "https://proxy.example.com/custom/chat/completions",
        }, clear=False):
            settings = Settings.from_env(require_targets=False, require_mail=False)

        self.assertEqual(
            settings.llm_endpoint,
            "https://proxy.example.com/custom/chat/completions",
        )

    def test_invalid_extra_body_is_rejected(self):
        with patch.dict(os.environ, {"LLM_EXTRA_BODY_JSON": "[]"}, clear=False):
            with self.assertRaisesRegex(ValueError, "JSON 对象"):
                Settings.from_env(require_targets=False, require_mail=False)


if __name__ == "__main__":
    unittest.main()
