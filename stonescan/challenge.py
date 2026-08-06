"""Recognising a bot-protection challenge — and doing nothing about it.

On 2026-08-06 the /health page showed ~192 slabware.com tenants as BROKEN. They were not
broken. Measured, one GET each, same browser UA, same client, same minute:

    euromarble.slabware.com    /FullInventory.aspx  200  server: Microsoft-IIS/10.0
    selectstone.slabware.com   /FullInventory.aspx  403  server: cloudflare
                                                         cf-mitigated: challenge
                                                         body: <title>Just a moment...</title>

The working tenants answer straight from IIS; the rest sit behind a Cloudflare interactive
managed challenge. Their robots.txt is a 404 — it publishes nothing — so the supplier's
"no automated access" is stated out of band, where `robots.py` cannot see it.

THIS MODULE ONLY EVER RECOGNISES. It does not solve, satisfy, wait out, retry past or
otherwise defeat a challenge, and nothing downstream of it may either. A managed challenge is
the operator deliberately switching on bot mitigation; the project already treats a published
robots rule as a decision to respect, and this is the same statement made a different way.
Playwright exists in this codebase to render Angular SPAs, not to clear bot checks. If a
change ever makes a challenged host start returning data, that change is wrong.

Detection is header-first on purpose. `cf-mitigated: challenge` is Cloudflare's own explicit
label and needs no guessing. The body rule is a fallback for callers that already hold the
text, and is deliberately NOT used by the client hook: httpx response hooks fire on an unread
response, so reading a body there would break `client.stream()` for every provider.

Imports nothing from the package, so any module can use it without an import cycle.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

# Marker on the `last_error` of a challenged supplier, beside robots.py's BLOCK_MARKER.
# Matched by substring rather than prefix, because providers store the FORMATTED exception
# ("Challenged: challenge-blocked: …"), not the bare message — see robots.is_declined.
CHALLENGE_MARKER = "challenge-blocked:"

# Body fingerprints of the interstitial, used only where the text is already in hand. These
# identify the page; they are not parsed, followed or executed.
_BODY_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "_cf_chl_opt",
)


def challenge_error(detail: str) -> str:
    return f"{CHALLENGE_MARKER} {detail}"


def is_challenge_error(msg: str | None) -> bool:
    return CHALLENGE_MARKER in str(msg or "")


def detect(status_code: int, headers: Mapping[str, Any] | None, body: str = "") -> str:
    """Why this response is a bot-protection challenge, or "" if it is not.

    `body` is optional and only consulted when supplied; the client hook never passes it.
    """
    h = {str(k).lower(): str(v or "") for k, v in dict(headers or {}).items()}

    # PRIMARY: Cloudflare says so itself. No inference, no false-positive risk worth the name.
    if h.get("cf-mitigated", "").strip().lower() == "challenge":
        return "bot-protection challenge (cf-mitigated: challenge)"

    # SECONDARY: a 403 served BY Cloudflare whose body is the interstitial. Both halves are
    # required. A bare 403 from a Cloudflare-fronted origin is NOT enough — plenty of origins
    # sit behind Cloudflare and return an honest 403 for their own reasons, and misreading
    # that as "declined" would silently stop crawling a supplier who is merely erroring. The
    # asymmetry is deliberate: a false positive quietly drops a working catalogue, while a
    # false negative just leaves today's behaviour, which is the milder failure.
    if status_code == 403 and "cloudflare" in h.get("server", "").lower() and body:
        low = body[:4000].lower()
        if any(m in low for m in _BODY_MARKERS):
            return "bot-protection challenge (Cloudflare interstitial)"
    return ""


class Challenged(httpx.HTTPError):
    """Raised instead of returning a challenge page as if it were a catalog.

    Subclasses `httpx.HTTPError` for the same reason `robots.Disallowed` does: every provider
    already wraps its fetches in `except httpx.HTTPError`, so a challenged host skips exactly
    like a failed request would rather than exploding the run — while the marker on the
    message keeps it out of the retry pass and out of the empty-crawl streak.
    """

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(challenge_error(f"{reason}: {url}"))
        self.url = url
        self.reason = reason
