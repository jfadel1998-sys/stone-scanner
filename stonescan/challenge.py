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

# Statuses a challenge is served with. Also the ONLY statuses whose body the client hook
# will read: nothing legitimate is being streamed from a refusal, so reading here cannot
# disturb a real download, and every other status is left untouched.
CHALLENGE_STATUSES = (403, 503)

# Body fingerprints of an interstitial. These identify the page; they are not parsed,
# followed or executed.
#
# Cloudflare is not the only vendor, and assuming it was is what made this detector blind
# outside the US. Measured on rkmarblesindia.com: HTTP 403, `server: hcdn` (Hostinger), body
# "Checking your browser before accessing. Just a moment..." — invisible to a rule that
# required `server: cloudflare`. Sucuri, Imperva/Incapsula and DataDome are equally invisible.
#
# Chosen to be SPECIFIC, because the errors are asymmetric: a false positive silently stops
# crawling a supplier who is merely erroring, while a false negative just leaves the old
# behaviour. So these are interstitial-only phrases and vendor script names — never bare
# words like "denied", "forbidden" or "blocked", which any honest 403 page contains.
_BODY_MARKERS = (
    # Cloudflare
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "_cf_chl_opt",
    "cf_chl_",
    # Generic "prove you are a browser" interstitials (Hostinger hcdn, Sucuri, others)
    "checking your browser",
    "checking if the site connection is secure",
    "enable javascript and cookies to continue",
    "verifying you are human",
    "please verify you are a human",
    # Named vendors, from their own markup
    "incapsula incident id",
    "_incapsula_resource",
    "sucuri website firewall",
    "datadome",
    "perimeterx",
    "px-captcha",
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

    # SECONDARY: a refusal status whose body is an interstitial. NOT restricted to Cloudflare
    # any more — that restriction is what made this blind to every other WAF, and the vendor
    # is not the point. Both halves are still required: the status must be a refusal AND the
    # body must carry a marker, so a bare 403 from any origin is not enough. Plenty of hosts
    # return an honest 403 for their own reasons, and misreading that as "declined" would
    # silently stop crawling a supplier who is merely erroring.
    if status_code in CHALLENGE_STATUSES and body:
        low = body[:4000].lower()
        for m in _BODY_MARKERS:
            if m in low:
                vendor = h.get("server", "").strip() or "unknown"
                return f"bot-protection challenge (interstitial, server: {vendor})"
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
