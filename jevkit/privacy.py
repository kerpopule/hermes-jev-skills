"""The outbound boundary. Everything sent to Jev passes through here first.

Two tools: ``redact`` masks things that look like secrets or contact details, and
``is_sensitive`` says "do not send this at all". Callers that get a True from
``is_sensitive`` must skip Jev and take their fail-open path.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

_SECRET_WORDS = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|authorization\s*:|bearer\s+[a-z0-9._-]{8,}|password|passwd|"
    r"client[_ -]?secret|session[_ -]?cookie|credit[_ -]?card|card[_ -]?number|"
    r"\bcvv\b|\bssn\b|private[_ -]?key|BEGIN [A-Z ]*PRIVATE KEY)"
)
# An env-var name is how a secret usually appears in agent output: AWS_SECRET_ACCESS_KEY,
# STRIPE_SECRET, DB_PASSWORD, GITHUB_TOKEN. Matching only `secret_key` missed every one of
# them, because the revealing word sits in the middle of the name, not at its end.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)*[_-]"
    r"(?:SECRET|SECRET[_-]?\w*KEY|API[_-]?KEY|KEY|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|AUTH)\b"
    r"\s*[:=]\s*\S*"
)
_SECRET_NAME = re.compile(r"(?i)\bsecret[_ -](?:access[_ -])?key\b|\bsecret[_ -]?key\b")
_TOKEN_SHAPES = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|apikey_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})\b"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
# The keyword rules above only fire on a label. A bank alert or an order receipt carries
# the card number with no trigger word anywhere near it, and "4111 1111 1111 1111" went
# out verbatim. Luhn is what keeps this from eating order and reference numbers — the
# same trap the _TRACKING comment below documents, reached from the other direction.
_CARD = re.compile(r"(?<![\d.-])(?:\d[ -]?){12,18}\d(?![\d.-])")
# _PHONE is a North American shape: three, three, four. Two lines of a European signature
# ("+44 20 7946 0958", "+33 1 70 18 99 00") walked straight past it.
_INTL_PHONE = re.compile(r"(?<![\d+])\+\d{1,3}[\s.-]?(?:\d[\s.-]?){7,13}\d(?!\d)")
# A credential with no label at all: an AWS secret access key is 40 base64 characters and
# the word "secret" never appears beside it in a mail. Mixed case AND a digit is what
# separates it from a word, a hex digest (already [hex] by the time this runs) or a slug.
_HIGH_ENTROPY = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/=_-])")
# A UPS tracking number's digit tail parses as country-code + 3 + 3 + 4, so the phone rule
# ate it: "1Z999AA10123456784" became "1Z999AA[phone]". Those numbers are the operational
# spine of a shipping desk and a redactor that silently destroys them looks like it worked.
#
# The first fix was to widen the phone lookbehind to exclude letters too. That was wrong:
# it stopped redacting "x8505550134", "ext8505550134" and "Phone8505550134" — trading a
# data-loss bug for a privacy leak. Protect the specific thing instead of blunting the
# general rule. Pure-digit carrier formats (FedEx 12/15/20, USPS 20-22) are already safe,
# because the phone pattern's trailing (?!\d) refuses to match a prefix of a longer run.
_TRACKING = re.compile(r"\b1Z[0-9A-Z]{16}\b", re.IGNORECASE)
_LONG_HEX = re.compile(r"\b[a-fA-F0-9]{32,}\b")


def _luhn(digits: str) -> bool:
    """The check digit every payment card carries. An order number almost never passes it."""
    total, alternate = 0, False
    for char in reversed(digits):
        value = ord(char) - 48
        if alternate:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        alternate = not alternate
    return total % 10 == 0


def _mask_card(match: "re.Match[str]") -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)


def _mask_credential(match: "re.Match[str]") -> str:
    run = match.group(0)
    mixed = (any(c.isupper() for c in run) and any(c.islower() for c in run)
             and any(c.isdigit() for c in run))
    return "[secret]" if mixed else run


def normalize(text: str) -> str:
    """Fold look-alike and invisible characters so a gate cannot be dodged with Unicode."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) not in {"Cf", "Cc"} or c in "\n\t")


def is_sensitive(text: str) -> bool:
    probe = normalize(text)
    return bool(_SECRET_WORDS.search(probe) or _SECRET_NAME.search(probe)
                or _SECRET_ASSIGNMENT.search(probe) or _TOKEN_SHAPES.search(probe))


def redact(text: str, limit: int = 4000) -> str:
    out = normalize(text)
    # Hold tracking numbers aside so the phone rule cannot reach their digits, then put
    # them back before any truncation can cut a placeholder in half.
    held: List[str] = []

    def _hold(match: "re.Match[str]") -> str:
        held.append(match.group(0))
        return f"\x00TRK{len(held) - 1}\x00"

    out = _TRACKING.sub(_hold, out)
    out = _TOKEN_SHAPES.sub("[secret]", out)
    # Keep the variable's NAME (it is often the useful signal) and mask only its value.
    out = _SECRET_ASSIGNMENT.sub(lambda m: re.split(r"[:=]", m.group(0), maxsplit=1)[0].rstrip() + "=[secret]", out)
    out = _LONG_HEX.sub("[hex]", out)
    # After [hex], so a digest stays a digest, and before the phone rules, so a spaced
    # card number is not shredded into a "phone" and a remainder.
    out = _HIGH_ENTROPY.sub(_mask_credential, out)
    out = _CARD.sub(_mask_card, out)
    out = _EMAIL.sub("[email]", out)
    out = _PHONE.sub("[phone]", out)
    out = _INTL_PHONE.sub("[phone]", out)
    for index, value in enumerate(held):
        out = out.replace(f"\x00TRK{index}\x00", value)
    if len(out) > limit:
        half = max(limit, 0) // 2
        # out[-0:] is all of out, so a cap of 0 or 1 keeps no tail rather than the whole text.
        out = out[:half] + "\n[…]\n" + (out[-half:] if half else "")
    return out
