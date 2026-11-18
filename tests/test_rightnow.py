import copy
import io
import json
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from jevkit import client, rightnow as rn, rightnow_media as media

KEY = "rp_" + "synthetic-test-only" * 2
Q = {"ready": client.noul("Is the synthetic item ready?")}
REPLY = {"model": "rightnow-decision", "id": "synthetic-only", "usage": {"cost": 0.0002, "input_tokens": 10},
         "answers": {"ready": {"type": "noul", "noul": 0.9}}}


class RightNowProtocol(unittest.TestCase):
    def test_explicit_only_no_existing_provider_changes(self):
        from jevkit import keystore
        self.assertNotIn("rightnow", keystore.PROVIDERS)
        with patch.object(rn, "_send") as send:
            with self.assertRaisesRegex(client.JevError, "authorization_required"):
                rn.ask("synthetic", Q, api_key=KEY)
            send.assert_not_called()

    def test_exact_wire_no_retry_or_retention(self):
        calls = []
        def wire(body, headers, timeout):
            calls.append(json.loads(body))
            self.assertNotIn("Idempotency-Key", headers)
            self.assertEqual(headers["Authorization"], "Bearer " + KEY)
            return json.dumps(REPLY).encode()
        out = rn.ask("synthetic", Q, api_key=KEY, authorized=True, transport=wire)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], rn.MODEL)
        self.assertEqual(out["provider"], "rightnow")
        self.assertEqual(out["calibration"], "unqualified")

    def test_preserves_choice_definitions_and_score_levels(self):
        qs = {"pick": client.choice("Choose", {"ok": "safe synthetic", "no": "not safe"}),
              "score": client.score("Rate", ["poor", "good"])}
        out = rn.request("synthetic", qs)["questions"]
        self.assertEqual(out["pick"]["options"], ["ok", "no"])
        self.assertIn("safe synthetic", out["pick"]["instructions"])
        self.assertEqual(out["score"]["levels"], ["poor", "good"])

    def test_limits_refused_before_transport(self):
        bads = [{"b": client.box("Locate")},
                {"c": client.choice("Choose", {str(i): str(i) for i in range(11)})},
                {"s": client.score("Rate", [str(i) for i in range(5)])},
                {str(i): client.noul("Yes?") for i in range(33)},
                {"s": client.noul({"structured": "not supported"})}]
        for qs in bads:
            with self.subTest(qs=qs), patch.object(rn, "_send") as send:
                with self.assertRaises(client.JevError):
                    rn.ask("synthetic", qs, authorized=True, api_key=KEY)
                send.assert_not_called()
        with self.assertRaises(client.JevError):
            rn.request("x" * rn.MAX_BODY, Q)
        with self.assertRaises(client.JevError):
            rn.request({"bad": float("nan")}, Q)

    def test_malformed_refused_not_manufactured(self):
        for raw in [b"null", b"[]", b"{}", b"not json", b'{"answers":{},"answers":{}}']:
            with self.subTest(raw=raw), self.assertRaises(client.JevError):
                rn.validate(raw, Q)

    def test_partial_abstention_nonfinite_wrongmodel_costs(self):
        for mutate in [lambda r: r["answers"]["ready"].update(abstained=True),
                       lambda r: r["answers"]["ready"].update(error={"code": "refused"}),
                       lambda r: r["answers"]["ready"].update(noul=float("nan")),
                       lambda r: r.update(model="unrelated"),
                       lambda r: r.update(usage={}),
                       lambda r: r["usage"].update(cost=-1),
                       lambda r: r["usage"].update(cost=True),
                       lambda r: r["usage"].update(cost=float("inf")),
                       lambda r: r["answers"].update(extra={"type": "noul", "noul": 1})]:
            reply = copy.deepcopy(REPLY)
            mutate(reply)
            with self.assertRaises(client.JevError):
                rn.validate(json.dumps(reply).encode(), Q)

    def test_choice_and_score_strict_validation(self):
        qs = {"c": client.choice("Choose", {"a": "first", "b": "second"}),
              "s": client.score("Rate", ["low", "high"])}
        reply = {"model": rn.MODEL, "usage": {"cost": 0}, "answers": {
            "c": {"type": "choice", "choice": "a", "confidence": .9, "probabilities": {"a": .9, "b": .1}},
            "s": {"type": "score", "score": .8, "confidence": .8, "probabilities": {"0": .2, "1": .8}}}}
        rn.validate(json.dumps(reply).encode(), qs)
        for field, value in [("confidence", .5), ("choice", "b"), ("probabilities", {"a": .9})]:
            bad = copy.deepcopy(reply); bad["answers"]["c"][field] = value
            with self.assertRaises(client.JevError): rn.validate(json.dumps(bad).encode(), qs)
        bad = copy.deepcopy(reply); bad["answers"]["s"].pop("probabilities")
        with self.assertRaises(client.JevError): rn.validate(json.dumps(bad).encode(), qs)

    def test_key_and_timeout_bounds(self):
        for key in ["", "rnsk_earlier-key-not-public", "rp_" + "a" * 20 + "\n"]:
            with self.assertRaises(client.JevError):
                rn.ask("synthetic", Q, authorized=True, api_key=key)
        for timeout in [True, 0, -1, float("nan"), 131]:
            with self.assertRaises(client.JevError):
                rn.ask("synthetic", Q, authorized=True, api_key=KEY, timeout=timeout)
        def late(*args):
            time.sleep(.02)
            return json.dumps(REPLY).encode()
        with self.assertRaisesRegex(client.JevError, "timeout"):
            rn.ask("synthetic", Q, authorized=True, api_key=KEY, timeout=.001, transport=late)

    def test_http_failures_are_sanitized_single_attempt(self):
        import urllib.error
        from email.message import Message
        for status, code in [(401, 'auth'), (402, 'credits'), (429, 'rate_limit'),
                             (503, 'unavailable'), (504, 'timeout')]:
            error = urllib.error.HTTPError(rn.ENDPOINT, status, 'private vendor text', Message(), None)
            with self.subTest(status=status), patch.object(rn.urllib.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(client.JevError) as caught:
                    rn.ask('synthetic', Q, authorized=True, api_key=KEY)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('private vendor text', str(caught.exception))
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_redirect_refuses_before_key_forwarding(self):
        with self.assertRaisesRegex(client.JevError, "redirect_refused"):
            rn._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid")


class EvaluationScripts(unittest.TestCase):
    def test_scorecard_is_offline_and_rejects_duplicates(self):
        import runpy
        summary = runpy.run_path(str(Path(__file__).parents[1] / 'scripts' / 'rightnow_scorecard.py'))['summarize']
        row = {'id': 'synthetic', 'provider': 'rightnow', 'result': {'latency_ms': 12,
               'usage': {'cost': .001, 'input_tokens': 10},
               'answers': {'check': {'type': 'noul', 'noul': .9}}}, 'expected': True, 'correct': True}
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'journal.jsonl'; p.write_text(json.dumps(row) + '\n')
            out = summary([p]); self.assertEqual(out['live_calls_made_by_summary'], 0)
            self.assertEqual(out['cohorts'][0]['n'], 1)
            self.assertAlmostEqual(out['cohorts'][0]['brier_noul'], .01)
            p.write_text((json.dumps(row) + '\n') * 2)
            with self.assertRaises(ValueError): summary([p])


class MediaPreflight(unittest.TestCase):
    def test_real_png_decode_after_exif_inspection(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('optional Pillow is not installed')
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'synthetic.png'
            Image.new('RGB', (32, 32), 'green').save(p)
            self.assertEqual(media.inline(p, 'image')['kind'], 'image')
            p.write_bytes(p.read_bytes()[:25])
            with self.assertRaises(client.JevError): media.inline(p, 'image')

    def test_actual_pcm_and_wrong_rate_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "synthetic.wav"
            for rate in [16000, 8000]:
                output = io.BytesIO()
                with wave.open(output, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(b"\0\0" * rate)
                p.write_bytes(output.getvalue())
                if rate == 16000:
                    prepared = media.inline(p, "audio")
                    self.assertEqual(prepared["fee_usd"], .006)
                    self.assertEqual(media.state("Inspect synthetic silence", [prepared])[1]["type"], "input_audio")
                    p.write_bytes(output.getvalue()[:-10])
                    with self.assertRaises(client.JevError): media.inline(p, "audio")
                else:
                    with self.assertRaises(client.JevError): media.inline(p, "audio")

    def test_pdf_requires_readable_unencrypted_text(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('optional pypdf is not installed')
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'blank.pdf'
            writer = PdfWriter(); writer.add_blank_page(width=100, height=100)
            with p.open('wb') as stream: writer.write(stream)
            with self.assertRaisesRegex(client.JevError, 'document_unsupported'):
                media.inline(p, 'pdf')

    def test_video_rejects_invalid_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / 'invalid.mp4'; p.write_bytes(b'synthetic not video')
            with self.assertRaises(client.JevError): media.inline(p, 'video')

    def test_mixing_and_size_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "oversize.bin"; p.write_bytes(b"0" * (rn.MAX_FILES + 1))
            with self.assertRaises(client.JevError): media.inline(p, "image")
        for prepared in [[{"kind": "image", "bytes": 1}, {"kind": "audio", "bytes": 1}],
                         [{"kind": "image", "bytes": 1}] * 5,
                         [{"kind": "audio", "bytes": 1}] * 2]:
            with self.assertRaises(client.JevError): media.state("Inspect", prepared)


if __name__ == "__main__":
    unittest.main()
