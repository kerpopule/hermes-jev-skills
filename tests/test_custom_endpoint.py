"""A local Jev-compatible endpoint never receives a saved provider credential."""
import json
import os
import unittest
from unittest import mock

from jevkit import client, keystore

QUESTION = {"ok": client.noul("Does the state report success?")}
REPLY = json.dumps({"answers": {"ok": {"type": "noul", "noul": 0.9}}}).encode()


class EndpointTests(unittest.TestCase):
    def test_local_endpoint_does_not_resolve_or_send_any_provider_key(self):
        captured = {}

        def transport(body, headers, timeout, url):
            captured.update(url=url, headers=headers)
            return REPLY

        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "http://127.0.0.1:8787/", "TYPESAFE_API_KEY": "private"}), \
             mock.patch.object(keystore, "resolve", side_effect=AssertionError("key lookup")), \
             mock.patch.object(client, "_http_transport", side_effect=transport):
            result = client.ask("safe synthetic state", QUESTION)
        self.assertEqual(result["answers"]["ok"]["noul"], 0.9)
        self.assertEqual(captured["url"], "http://127.0.0.1:8787/v1/systemone")
        self.assertNotIn("Authorization", captured["headers"])

    def test_explicit_other_provider_ignores_custom_endpoint(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "http://127.0.0.1:8787/"}), \
             mock.patch.object(keystore, "resolve", return_value="synthetic"), \
             mock.patch.object(client, "_openrouter_transport", return_value=REPLY) as send:
            client.ask("synthetic", QUESTION, provider="openrouter")
        self.assertEqual(send.call_count, 1)
        self.assertIn("Authorization", send.call_args.args[1])

    def test_rejects_invalid_or_remote_cleartext_endpoint_before_network(self):
        for url in ("http://example.org", "http://127.0.0.2:8787", "https://user:pass@example.org", "ftp://localhost", "https://example.org/path?token=x", "https://example.org/#frag", "http://[::1"):
            with self.subTest(url=url), mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": url}), \
                 mock.patch.object(client, "_http_transport") as send:
                with self.assertRaises(client.JevError) as caught:
                    client.ask("synthetic", QUESTION)
                self.assertEqual(caught.exception.code, "invalid_endpoint")
                send.assert_not_called()

    def test_official_url_and_key_path_unchanged_without_override(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(keystore, "resolve", return_value="synthetic"), \
             mock.patch.object(client, "_http_transport", return_value=REPLY) as send:
            client.ask("synthetic", QUESTION, provider="typesafe")
        self.assertEqual(send.call_args.args[1]["Authorization"], "Bearer synthetic")
        self.assertEqual(len(send.call_args.args), 3)

    def test_official_base_url_explicit_still_uses_normal_auth(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://api.typesafe.ai/"}), \
             mock.patch.object(keystore, "resolve", return_value="synthetic"), \
             mock.patch.object(client, "_http_transport", return_value=REPLY) as send:
            client.ask("synthetic", QUESTION, provider="typesafe")
        self.assertEqual(send.call_args.args[1]["Authorization"], "Bearer synthetic")

    def test_https_proxy_gets_env_key_only_never_a_stored_credential(self):
        captured = {}

        def transport(body, headers, timeout, url):
            captured.update(url=url, headers=headers)
            return REPLY

        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://proxy.example/", "TYPESAFE_API_KEY": "env-proxy"}), \
             mock.patch.object(keystore, "resolve", side_effect=AssertionError("key lookup")), \
             mock.patch.object(client, "_http_transport", side_effect=transport):
            client.ask("synthetic", QUESTION)
        self.assertEqual(captured["url"], "https://proxy.example/v1/systemone")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer env-proxy")
        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://proxy.example/"}, clear=True), \
             mock.patch.object(keystore, "resolve", side_effect=AssertionError("key lookup")), \
             mock.patch.object(client, "_http_transport", side_effect=transport):
            client.ask("synthetic", QUESTION)
        self.assertNotIn("Authorization", captured["headers"])
        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://gw.example/jev/"}, clear=True), \
             mock.patch.object(client, "_http_transport", side_effect=transport):
            client.ask("synthetic", QUESTION)
        self.assertEqual(captured["url"], "https://gw.example/jev/v1/systemone")

    def test_custom_server_cannot_claim_to_verify_a_typesafe_credential(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://proxy.example"}), \
             mock.patch.object(client, "ask", side_effect=AssertionError("must not send")):
            self.assertFalse(client.verify_key("synthetic"))


if __name__ == "__main__":
    unittest.main()
