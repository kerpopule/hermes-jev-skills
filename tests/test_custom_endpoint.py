"""A local Jev-compatible endpoint never receives a saved provider credential."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from jevkit import cli, client, keystore

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

    def capture(self, env):
        """One ask() against TYPESAFE_BASE_URL with only ``env`` set; the key store must stay unread."""
        captured = {}

        def transport(body, headers, timeout, url):
            captured.update(url=url, headers=headers)
            return REPLY

        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(keystore, "resolve", side_effect=AssertionError("key lookup")), \
             mock.patch.object(client, "_http_transport", side_effect=transport):
            client.ask("synthetic", QUESTION)
        return captured

    def test_a_gateway_path_prefix_is_part_of_the_endpoint(self):
        """#23/#24: a gateway that mounts the API under a prefix could not be named at all."""
        for base, url in (("https://gw.example/jev", "https://gw.example/jev/v1/systemone"),
                          ("https://gw.example/jev/", "https://gw.example/jev/v1/systemone"),
                          ("https://gw.example:8443/team/jev", "https://gw.example:8443/team/jev/v1/systemone"),
                          ("http://127.0.0.1:4000/jev", "http://127.0.0.1:4000/jev/v1/systemone"),
                          ("https://gw.example/", "https://gw.example/v1/systemone")):
            with self.subTest(base=base):
                self.assertEqual(self.capture({"TYPESAFE_BASE_URL": base})["url"], url)

    def test_a_path_prefix_that_is_not_plain_segments_is_refused_before_network(self):
        for base in ("https://gw.example/jev/../admin", "https://gw.example/./jev", "https://gw.example//evil.example",
                     "https://gw.example/a%2e%2e", "https://gw.example/je v", "https://gw.example/a\\b",
                     "http://example.org/jev"):
            with self.subTest(base=base), mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": base}), \
                 mock.patch.object(client, "_http_transport") as send:
                with self.assertRaises(client.JevError) as caught:
                    client.ask("synthetic", QUESTION)
                self.assertEqual(caught.exception.code, "invalid_endpoint")
                send.assert_not_called()

    def test_the_environment_provider_key_is_never_forwarded_to_an_https_gateway(self):
        """The #15 guarantee holds for an https override too: TYPESAFE_API_KEY is a provider key."""
        captured = self.capture({"TYPESAFE_BASE_URL": "https://gw.example/jev", "TYPESAFE_API_KEY": "real-provider-key"})
        self.assertNotIn("Authorization", captured["headers"])
        self.assertNotIn("real-provider-key", json.dumps(captured))

    def test_the_proxy_key_is_the_only_bearer_an_override_can_receive(self):
        captured = self.capture({"TYPESAFE_BASE_URL": "https://gw.example/jev",
                                 "TYPESAFE_API_KEY": "real-provider-key", client.PROXY_KEY_ENV: " gateway-key "})
        self.assertEqual(captured["url"], "https://gw.example/jev/v1/systemone")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer gateway-key")
        captured = self.capture({"TYPESAFE_BASE_URL": "http://127.0.0.1:4000/jev", client.PROXY_KEY_ENV: "local-gw"})
        self.assertEqual(captured["headers"]["Authorization"], "Bearer local-gw")

    def test_the_proxy_key_never_reaches_an_official_provider(self):
        with mock.patch.dict(os.environ, {client.PROXY_KEY_ENV: "gateway-key"}, clear=True), \
             mock.patch.object(keystore, "resolve", return_value="synthetic"), \
             mock.patch.object(client, "_http_transport", return_value=REPLY) as send:
            client.ask("synthetic", QUESTION, provider="typesafe")
        self.assertEqual(send.call_args.args[1]["Authorization"], "Bearer synthetic")

    def test_an_explicit_api_key_still_does_not_reach_an_override(self):
        captured = {}

        def transport(body, headers, timeout, url):
            captured.update(headers=headers)
            return REPLY
        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://gw.example/jev"}, clear=True), \
             mock.patch.object(client, "_http_transport", side_effect=transport):
            client.ask("synthetic", QUESTION, api_key="explicit-provider-key")
        self.assertNotIn("Authorization", captured["headers"])

    def test_custom_server_cannot_claim_to_verify_a_typesafe_credential(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_BASE_URL": "https://proxy.example"}), \
             mock.patch.object(client, "ask", side_effect=AssertionError("must not send")):
            self.assertFalse(client.verify_key("synthetic"))


class DoctorOverrideTests(unittest.TestCase):
    """A gateway's 401 read as "Jev had no opinion" everywhere; doctor now says what happened."""

    def doctor(self, env, key=False, reply=REPLY, offline=False):
        calls = []

        def transport(body, headers, timeout, url):
            calls.append((url, headers.get("Authorization")))
            if isinstance(reply, Exception):
                raise reply
            return reply
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(cli.keystore, "describe", return_value={"present": key}), \
             mock.patch.object(keystore, "resolve", side_effect=AssertionError("key lookup")), \
             mock.patch.object(client, "_http_transport", side_effect=transport), \
             mock.patch.object(cli.route, "load_config", return_value={"mode": "redacted-text", "tiers": {}}), \
             contextlib.redirect_stdout(out):
            code = cli.main(["doctor"] + (["--offline"] if offline else []))
        return code, json.loads(out.getvalue()), calls

    def test_a_reachable_gateway_passes_without_a_stored_key_and_names_its_bearer(self):
        code, report, calls = self.doctor({"TYPESAFE_BASE_URL": "https://gw.example/jev",
                                           client.PROXY_KEY_ENV: "gateway-key"})
        self.assertEqual(code, 0)
        self.assertEqual(report["endpoint_override"], {"url": "https://gw.example/jev/v1/systemone",
                                                       "valid": True, "bearer": client.PROXY_KEY_ENV})
        self.assertTrue(report["jev"]["reachable"])
        self.assertEqual(calls, [("https://gw.example/jev/v1/systemone", "Bearer gateway-key")])
        self.assertNotIn("gateway-key", json.dumps(report))

    def test_a_gateway_that_refuses_the_call_fails_doctor_and_says_why(self):
        code, report, _ = self.doctor({"TYPESAFE_BASE_URL": "https://gw.example/jev"}, key=True,
                                      reply=client.JevError("auth_failed"))
        self.assertEqual(code, 1, "a stored key does not help: the override never receives it")
        self.assertEqual(report["jev"], {"reachable": False, "error": "auth_failed"})
        self.assertIsNone(report["endpoint_override"]["bearer"])

    def test_an_invalid_override_fails_doctor_without_touching_the_network(self):
        code, report, calls = self.doctor({"TYPESAFE_BASE_URL": "http://example.org/jev"}, key=True)
        self.assertEqual(code, 1)
        self.assertFalse(report["endpoint_override"]["valid"])
        self.assertEqual(calls, [])
        self.assertIn("jev", report, "the call was made and refused before any network")
        self.assertEqual(report["jev"]["error"], "invalid_endpoint")

    def test_no_override_leaves_doctor_as_it_was(self):
        code, report, calls = self.doctor({}, key=False, offline=True)
        self.assertEqual(code, 1)
        self.assertNotIn("endpoint_override", report)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
