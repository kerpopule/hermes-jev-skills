---
name: rightnow-media-triage
description: Use when evaluating multimodal decision triage.
---

# Multimodal decision triage

Use only within a requested RightNow evaluation with explicit data and spending scope. This custom procedure borrows vendor input patterns, not vendor operating instructions. Load `rightnow-evaluation` first. No credentials or private media in this skill.

## Prepare locally, then selectively ask

- Images: upright, static PNG/JPEG, max four, <=16,777,216 pixels per image, aspect <=50:1. Inspect the actual export. Use `jevkit.rightnow_media.inline(path, 'image')`, then `state(text, prepared)`. Requires existing Pillow. Reject animated inputs and EXIF rotation instead of silently modifying them.
- Audio: this helper intentionally supports PCM WAV only, exactly 16kHz, <=60 seconds, byte-limited. Convert explicitly with existing ffmpeg if needed, listen to the export, then `inline(path, 'audio')`. Float WAV is vendor-supported but not helper-qualified. One recording alone, no images mixed. A short clip still incurs the 30-second minimum.
- PDF: existing pypdf, unencrypted readable text, <=500 physical pages. `inline(path, 'pdf')`. Images may be omitted by the service. A scanned PDF is NOT visual OCR verification; render selected pages locally and use inspected image exports or local OCR first.
- Video: existing ffprobe, one silent upright H.264 MP4, <=10s, <=1920x1080, <=2.5MiB. `inline(path, 'video')`. Inspect the actual clip. No soundtrack, rotation or other files. Video is vendor preview; a refusal is a capability gap, not an excuse for undocumented endpoints or repeated paid retries.

All files combined <=2,621,440 decoded bytes; whole JSON <=3,665,920 bytes. The helper enforces a conservative single-family request. No remote URLs, upload service or background watcher. Bytes remain local until an explicitly authorized `rightnow.ask`. Helpers verify encoding/format, not useful perception or privacy.

## Good bounded applications

1. Frame/contact-sheet triage: choose likely defects or scene boundaries for deeper local/full-model review. Test flicker and brief violations against labeled clips; sparse frames cannot certify absence of a defect between frames.
2. UI state verification: compare expected visible label/state using public/synthetic screenshots. Include unreadable/uncertain. Never turn visual confidence into a click permission; prevalidate actions and preserve the existing Jev action workflow until qualified.
3. Audio routing: distinguish silence, likely speech or music and select an appropriate local transcription/review path. It does not write transcripts or clone voices. Voice/face/customer isolation remains unchanged.
4. Document checks: decide whether extracted text contains a required field or whether two explicitly supplied values agree. Use deterministic arithmetic, text checks and parser validation first; this is not financial, legal, clinical or safety certification.

## Acceptance

Run actual synthetic success, wrong-format, oversize, no-readable-text, mixed-media and timeout cases. Record modality separately as local-preflight-tested / API-accepted / semantic-quality-accepted. No single aggregate 'multimodal works' label. Image/video/audio/PDF each need known-answer positive, negative and ambiguous fixtures, model identity, actual usage and false-positive rates. Use context-local thresholds and abstention; never inherit Jev's calibrated thresholds blindly.

Source: official RightNow `/docs/decision-model/attachments`, `/quickstart`, `/pricing`, inspected 2026-11-18. No raw vendor skill or SDK installed.
