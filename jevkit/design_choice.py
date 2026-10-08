"""Bounded design-choice example. Code owns tokens, content and rendering."""
from __future__ import annotations

from html import escape

from . import client, privacy

# Example policy only: this threshold is not a calibrated design-quality claim.
MIN_CONFIDENCE = 0.85

PRESETS = {
    'product': {'layout': 'grid', 'type': 'sans', 'palette': 'midnight',
                'background': '#101827', 'foreground': '#f8fafc', 'accent': '#93c5fd'},
    'editorial': {'layout': 'stack', 'type': 'serif', 'palette': 'paper',
                  'background': '#fffaf0', 'foreground': '#242424', 'accent': '#684321'},
}


def select(intent: str, *, live: bool = False, transport=None) -> dict:
    """Pick one complete compatible preset. No page content enters this API.

    Offline is the default. ``live`` must be explicitly enabled by the caller;
    ``transport`` exists for offline contract tests using the real client validator.
    """
    if not isinstance(intent, str) or intent not in PRESETS:
        raise ValueError('intent must be product or editorial')
    fallback = {'preset': intent, 'source': 'code', 'reason': 'offline'}
    if not live:
        return fallback
    try:
        result = client.ask(
            {'intent': privacy.redact(intent, 32)},
            {'preset': client.choice('Choose the complete preset matching the stated intent.', {
                'product': 'Product interface: grid layout, sans typography, midnight palette.',
                'editorial': 'Editorial reading: stacked layout, serif typography, paper palette.',
            })}, timeout=2.0, retries=0, transport=transport)
        answer = result['answers']['preset']
        if answer['confidence'] < MIN_CONFIDENCE:
            return {'preset': intent, 'source': 'fallback', 'reason': 'uncertain'}
        return {'preset': answer['choice'], 'source': 'jev', 'reason': 'selected'}
    except client.JevError as error:
        # Stable code only, never transport error text or credentials.
        return {'preset': intent, 'source': 'fallback', 'reason': error.code}


def contrast(preset: str, *, accent: bool = False) -> float:
    """WCAG sRGB contrast for the preset's text/background (or accent)."""
    p = PRESETS[preset]
    def luminance(color):
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    a, b = sorted((luminance(p['accent' if accent else 'foreground']),
                   luminance(p['background'])))
    return (b + 0.05) / (a + 0.05)


def render(preset: str, title: str, body: str) -> str:
    if not isinstance(preset, str) or preset not in PRESETS:
        raise ValueError('unknown preset')
    for name, value, limit in [('title', title, 200), ('body', body, 4000)]:
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f'{name} must contain 1 to {limit} characters')
    if min(contrast(preset), contrast(preset, accent=True)) < 4.5:
        raise ValueError('preset contrast must be at least 4.5:1')
    p = PRESETS[preset]
    font = 'Georgia, serif' if p['type'] == 'serif' else 'system-ui, sans-serif'
    columns = '1fr 1fr' if p['layout'] == 'grid' else '1fr'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Bounded design choice</title>
<style>
* {{box-sizing:border-box}} body {{margin:0;background:{p['background']};color:{p['foreground']};font-family:{font}}}
main {{max-width:1040px;margin:auto;padding:clamp(20px,5vw,72px)}}
section {{display:grid;grid-template-columns:{columns};gap:32px}}
section > * {{min-width:0}} h1 {{font-size:clamp(32px,5vw,64px);line-height:1.08}}
h1,p {{overflow-wrap:anywhere}} p {{font-size:20px;line-height:1.6}}
.label {{color:{p['accent']};font:600 14px system-ui,sans-serif;letter-spacing:.12em}}
@media(max-width:600px) {{section {{grid-template-columns:1fr}}}}
</style></head><body><main><p class="label">DESIGN CHOICE / {escape(preset.upper())}</p>
<section><h1>{escape(title)}</h1><p>{escape(body)}</p></section>
</main></body></html>'''


def main() -> int:
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--intent', choices=sorted(PRESETS), default='product')
    parser.add_argument('--title', default='Good decisions. Real rendering.')
    parser.add_argument('--body', default='Jev picks a vetted preset. Code owns the layout, typography and palette. Local checks verify the rendered result.')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--live', action='store_true', help='Opt in to a provider call; may incur charges. Sends enum intent only.')
    args = parser.parse_args()
    try:
        # Validate copy before any opt-in provider call. It never enters select().
        render(args.intent, args.title, args.body)
        with args.out.open('x', encoding='utf-8') as output:
            result = select(args.intent, live=args.live)
            html = render(result['preset'], args.title, args.body)
            output.write(html)
    except (ValueError, OSError) as error:
        parser.exit(2, f'{type(error).__name__}: cannot create preview; check copy and output path\n')
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
