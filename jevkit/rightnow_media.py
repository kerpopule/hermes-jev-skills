"""Local media preflight for RightNow's public /decisions endpoint.

No network, conversion, upload or remote URLs. Optional Pillow/pypdf and
ffprobe must already be installed. Decoding verifies format, not semantic QA.
"""
from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path
import subprocess
import wave

from .client import JevError
from .rightnow import MAX_FILES


def inline(path, kind):
    """Inspect bounded local bytes, then return an inline state item and fee.

    fee_usd excludes text/questions/call. Only one media family per request
    is supported by this conservative adapter. All source files stay intact.
    """
    p = Path(path)
    with p.open("rb") as stream:
        data = stream.read(MAX_FILES + 1)
    if not data or len(data) > MAX_FILES:
        raise JevError("media_too_large")
    encoded = base64.b64encode(data).decode("ascii")
    if kind == "image":
        try:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                fmt, size = image.format, image.size
                if fmt not in ("PNG", "JPEG") or getattr(image, "is_animated", False):
                    raise ValueError()
                w, h = size
                if min(size) <= 0 or w * h > 16_777_216 or max(size) / min(size) > 50:
                    raise ValueError()
                if image.getexif().get(274, 1) != 1:
                    raise ValueError()
                # PNG EXIF inspection can load the stream. Verify a fresh
                # decoder, then fully decode the same immutable bytes too.
                with Image.open(io.BytesIO(data)) as verifier:
                    verifier.verify()
                with Image.open(io.BytesIO(data)) as decoder:
                    decoder.load()
        except ImportError:
            raise JevError("dependency", "image preflight needs Pillow") from None
        except Exception:
            raise JevError("image_unsupported", "requires upright static PNG/JPEG within limits") from None
        mime = "image/png" if fmt == "PNG" else "image/jpeg"
        item = {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + encoded}}
        fee = 0.003
    elif kind == "audio":
        try:
            with wave.open(io.BytesIO(data)) as audio:
                duration = audio.getnframes() / audio.getframerate()
                if audio.getframerate() != 16000 or not 0 < duration <= 60:
                    raise ValueError()
                if audio.getcomptype() != "NONE" or audio.getsampwidth() not in (1, 2, 3, 4):
                    raise ValueError()
                expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                if len(audio.readframes(audio.getnframes())) != expected:
                    raise ValueError()
        except Exception:
            raise JevError("audio_unsupported", "requires complete PCM WAV, 16000Hz, at most 60s") from None
        item = {"type": "input_audio", "input_audio": {"data": encoded, "format": "wav"}}
        fee = max(30, math.ceil(duration)) * 0.0002
    elif kind == "pdf":
        try:
            from pypdf import PdfReader
            pdf = PdfReader(io.BytesIO(data), strict=True)
            if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 500:
                raise ValueError()
            pages = len(pdf.pages)
            if not any((page.extract_text() or "").strip() for page in pdf.pages):
                raise ValueError()
        except ImportError:
            raise JevError("dependency", "PDF preflight needs pypdf") from None
        except Exception:
            raise JevError("document_unsupported", "requires unencrypted PDF with readable text") from None
        item = {"type": "file", "file": {"filename": "document.pdf", "file_data": "data:application/pdf;base64," + encoded}}
        fee = pages * 0.002
    elif kind == "video":
        # Probe the exact bytes encoded, not a path that another process can replace.
        try:
            proc = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", "pipe:0"],
                                  input=data, capture_output=True, timeout=10, check=True)
            info = json.loads(proc.stdout)
            streams = info["streams"]
            video = [s for s in streams if s.get("codec_type") == "video"]
            duration = float(info["format"]["duration"])
            if len(video) != 1 or len(streams) != 1 or video[0].get("codec_name") != "h264":
                raise ValueError()
            if "mp4" not in info["format"]["format_name"] or data[4:8] != b"ftyp":
                raise ValueError()
            if not 0 < duration <= 10 or not 0 < video[0]["width"] <= 1920 or not 0 < video[0]["height"] <= 1080:
                raise ValueError()
            rotations = [video[0].get("tags", {}).get("rotate", 0)]
            rotations += [s.get("rotation", 0) for s in video[0].get("side_data_list", [])]
            if any(float(r) != 0 for r in rotations):
                raise ValueError()
        except FileNotFoundError:
            raise JevError("dependency", "video preflight needs ffprobe") from None
        except Exception:
            raise JevError("video_unsupported", "requires silent upright H264 MP4, <=10s, <=1920x1080") from None
        item = {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + encoded}}
        fee = max(0.008, math.ceil(duration) * 0.0008)
    else:
        raise JevError("media_unsupported")
    return {"kind": kind, "item": item, "bytes": len(data), "fee_usd": fee}


def state(text, prepared):
    """Assemble already-inspected local media. Full body bound is checked by request()."""
    if not isinstance(text, str) or not text.strip() or not prepared:
        raise JevError("invalid_request")
    kinds = {p["kind"] for p in prepared}
    if len(kinds) != 1 or len(prepared) > (4 if kinds == {"image"} else 1):
        raise JevError("media_unsupported", "do not mix files; maximum four images or one other file")
    if sum(p["bytes"] for p in prepared) > MAX_FILES:
        raise JevError("media_too_large")
    return [{"type": "text", "text": text}] + [p["item"] for p in prepared]
