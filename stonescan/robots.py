"""Honor robots.txt — the promise the rest of the project rests on.

Stone Scanner indexes ~120 public supplier catalogs that never asked to be
indexed. What makes that defensible is the claim in CLAUDE.md: we read the same
pages a customer reads, we identify ourselves, and *we obey robots.txt*. Until
this module that claim was true of exactly one provider (`genericfeed`);
everything else fetched whatever it liked, and "we honor robots.txt" really meant
"a human read it once at seeding time".

The check lives at the HTTP layer rather than as a per-supplier preflight on
purpose. A supplier entry's `host` is frequently NOT the host that gets fetched:

    slabcloud     entry `foo.slabcloud.com`      -> fetches slabcloud.com/api/...
    umi           entry `umistone.com`           -> fetches apps.umistone.com/linv/...
    stoneprofits  entry `x.stoneprofitsweb.com`  -> that AND <token>.stoneprofits.com/api/...

so a preflight keyed on the entry host would check an origin we barely touch and
quietly wave through the request that actually carries the catalog. `PoliteClient`
checks the URL being fetched, per origin, which also makes the next provider
robots-clean for free instead of by remembering.

Semantics follow RFC 9309:

  * the longest matching `User-agent` group wins; `*` is only the fallback,
  * within a group the longest matching path pattern wins, and `Allow` beats
    `Disallow` on an equal-length tie. That tie-break is load-bearing, not
    pedantry: unbuilt.co disallows `/api/` but allows `/api/listings/`, which is
    the single endpoint that provider is built on,
  * `*` (any run) and `$` (end anchor) are honored in path patterns, and the
    pattern is matched against path+query,
  * 4xx -> there is no robots file; everything is allowed,
  * 5xx / timeout / DNS failure -> "unreachable", which the RFC says to treat as
    a full disallow. We do — but report it as `unreachable` rather than `blocked`
    so ingest can log a retryable error instead of recording a supplier as having
    opted out when really our network hiccuped.

`Crawl-delay` is parsed and surfaced; callers take max(their own delay, this).
"""

from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .challenge import CHALLENGE_MARKER, CHALLENGE_STATUSES, Challenged
from .challenge import detect as detect_challenge
from .challenge import is_challenge_error

# The product token suppliers would write in their robots.txt to address us, and
# the full UA we send. Keep these two in step — crawler.py sends its own Playwright
# UA string built from the same token.
AGENT_TOKEN = "StoneScanner"
USER_AGENT = (
    f"Mozilla/5.0 (compatible; {AGENT_TOKEN}/0.1; "
    "+public-catalog indexer; contact: jason@traxtone.com)"
)

# A robots.txt is small; if a host can't answer in this long it counts as
# unreachable, and unreachable means we don't crawl it.
FETCH_TIMEOUT = 20.0

# Marks our own robots.txt requests so PoliteClient's hook lets them past without
# consulting the very file they are fetching.
_ROBOTS_FETCH = "stonescan_robots_fetch"

ALLOWED = "allowed"
NO_ROBOTS = "no-robots"      # 4xx: no file published, so nothing is restricted
BLOCKED = "blocked"          # an explicit Disallow matched — a real opt-out
UNREACHABLE = "unreachable"  # 5xx/network: we must not crawl, but it's retryable

# Prefix on the `last_error` a blocked supplier gets. A block is a decision, not a
# failure: the retry pass must skip these instead of hammering, every night, a host
# that has told us not to come back. Retrying an UNREACHABLE is right; retrying a
# BLOCKED is the same bug as re-crawling suppliers whose error never cleared.
BLOCK_MARKER = "robots-blocked:"


def block_error(detail: str) -> str:
    return f"{BLOCK_MARKER} {detail}"


def is_block_error(msg: str | None) -> bool:
    # SUBSTRING, not startswith. Providers store the FORMATTED exception —
    # `out.error = f"{type(e).__name__}: {e}"` — so a real block is stored as
    # "Disallowed: robots-blocked: …" and a prefix test never matched it. Every protection
    # this marker exists for (out of the retry pass, out of the empty-crawl streak, BLOCKED
    # rather than BROKEN on /health) was therefore dead on the provider path. It went
    # unnoticed because no host has yet published a Disallow we honour: 0 of 393 rows are
    # blocked today, so the bug had nothing to be wrong about.
    return CHALLENGE_MARKER not in str(msg or "") and BLOCK_MARKER in str(msg or "")


def is_declined(msg: str | None) -> bool:
    """Whether the supplier ANSWERED us and the answer was no — by robots.txt or by a
    bot-protection challenge. Both are decisions, not failures, and the difference between
    them matters only to what we show a human; every mechanism that must not hammer a host
    who already said no cares about the union."""
    return is_block_error(msg) or is_challenge_error(msg)


@dataclass(frozen=True)
class Decision:
    """Why one URL may or may not be fetched. `reason` distinguishes a supplier
    opting out (BLOCKED — permanent, honor it) from us failing to ask
    (UNREACHABLE — transient, retry it), which callers report very differently."""

    allowed: bool
    reason: str
    rule: str = ""          # the robots.txt line responsible, for logs/audits
    crawl_delay: float = 0.0

    def __str__(self) -> str:
        if self.allowed:
            return self.reason
        detail = f" ({self.rule})" if self.rule else ""
        return f"{self.reason}{detail}"


class Disallowed(httpx.HTTPError):
    """Raised instead of performing a fetch robots.txt forbids.

    Subclasses `httpx.HTTPError` deliberately: every provider already wraps its
    per-item fetches in `except httpx.HTTPError`, so a blocked URL skips that one
    item exactly like a failed request would, rather than exploding the run. The
    block is still counted on the client so it can be reported rather than lost.
    """

    def __init__(self, url: str, decision: Decision) -> None:
        msg = f"{decision} for {AGENT_TOKEN}: {url}"
        super().__init__(block_error(msg) if decision.reason == BLOCKED
                         else f"robots.txt {msg}")
        self.url = url
        self.decision = decision


# --- parsing -------------------------------------------------------------------

def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile a robots path pattern. Only `*` and a trailing `$` are special."""
    out: list[str] = []
    last = len(pattern) - 1
    for i, ch in enumerate(pattern):
        if ch == "*":
            out.append(".*")
        elif ch == "$" and i == last:
            out.append(r"\Z")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out))


@dataclass
class _Group:
    allows: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    disallows: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    crawl_delay: float = 0.0


class RobotsPolicy:
    """The rules of one origin's robots.txt, resolved for one agent token."""

    def __init__(self, group: _Group | None, reason: str = ALLOWED,
                 sitemaps: list[str] | None = None) -> None:
        self._group = group or _Group()
        self._reason = reason
        self._detail = ""
        self.sitemaps = sitemaps or []

    @property
    def crawl_delay(self) -> float:
        return self._group.crawl_delay

    @classmethod
    def unreachable(cls, detail: str = "") -> RobotsPolicy:
        """No answer from the server: RFC 9309 says assume a full disallow."""
        p = cls(None, reason=UNREACHABLE)
        p._detail = detail
        return p

    @classmethod
    def none_published(cls, detail: str = "") -> RobotsPolicy:
        p = cls(None, reason=NO_ROBOTS)
        p._detail = detail
        return p

    @classmethod
    def parse(cls, text: str, agent: str = AGENT_TOKEN) -> RobotsPolicy:
        groups: dict[str, _Group] = {}
        sitemaps: list[str] = []
        current: list[str] = []
        # Consecutive User-agent lines share one rule block; the first rule line
        # closes the header, so the next User-agent starts a fresh group.
        in_header = False

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field_name, _, value = line.partition(":")
            key = field_name.strip().lower()
            value = value.strip()

            if key == "sitemap":
                if value:
                    sitemaps.append(value)
                continue
            if key in ("user-agent", "useragent"):
                if not in_header:
                    current = []
                    in_header = True
                if value:
                    current.append(value.lower())
                    groups.setdefault(value.lower(), _Group())
                continue

            if key not in ("allow", "disallow", "crawl-delay"):
                continue
            in_header = False
            if not current:
                continue  # a rule before any User-agent line belongs to nobody
            for ua in current:
                g = groups[ua]
                if key == "crawl-delay":
                    try:
                        g.crawl_delay = max(g.crawl_delay, float(value))
                    except ValueError:
                        pass
                elif key == "allow":
                    if value:
                        g.allows.append((value, _pattern_regex(value)))
                elif value:
                    # "Disallow:" with an empty value explicitly allows everything,
                    # so only a non-empty value becomes a rule.
                    g.disallows.append((value, _pattern_regex(value)))

        return cls(_select_group(groups, agent), sitemaps=sitemaps)

    def allows(self, path: str) -> Decision:
        if self._reason == UNREACHABLE:
            return Decision(False, UNREACHABLE,
                            self._detail or "robots.txt could not be fetched")
        if self._reason == NO_ROBOTS:
            return Decision(True, NO_ROBOTS, self._detail, crawl_delay=self.crawl_delay)
        if not path.startswith("/"):
            path = "/" + path

        best_allow = _longest_match(self._group.allows, path)
        best_deny = _longest_match(self._group.disallows, path)
        if best_deny is None:
            return Decision(True, ALLOWED, crawl_delay=self.crawl_delay)
        # Longest pattern wins; Allow wins an exact-length tie (RFC 9309 2.2.2).
        if best_allow is not None and len(best_allow) >= len(best_deny):
            return Decision(True, ALLOWED, f"Allow: {best_allow}",
                            crawl_delay=self.crawl_delay)
        return Decision(False, BLOCKED, f"Disallow: {best_deny}",
                        crawl_delay=self.crawl_delay)


def _longest_match(rules: list[tuple[str, re.Pattern[str]]], path: str) -> str | None:
    best: str | None = None
    for pattern, rx in rules:
        if rx.match(path) and (best is None or len(pattern) > len(best)):
            best = pattern
    return best


def _select_group(groups: dict[str, _Group], agent: str) -> _Group | None:
    """The most specific group addressing us, else `*`, else no rules at all.

    Matching is the RFC's case-insensitive product-token comparison. We also
    accept a robots token that is merely *contained* in ours, which errs toward
    obeying more rules than strictly required — the safe direction for a crawler
    whose whole justification is being welcome.
    """
    token = agent.lower()
    best_name: str | None = None
    for name in groups:
        if name == "*":
            continue
        if name in token or token.startswith(name):
            if best_name is None or len(name) > len(best_name):
                best_name = name
    if best_name is not None:
        return groups[best_name]
    return groups.get("*")


# --- reviewed exceptions --------------------------------------------------------

@dataclass(frozen=True)
class Override:
    """A recorded, human-reviewed exception to one host's Disallow.

    Needed because a publisher's robots.txt can contradict their own server.
    unbuilt.co writes `Allow: /api/listings/` to carve that endpoint out of
    `Disallow: /api/`, then 308-redirects it to the slash-less `/api/listings`,
    which their Allow rule no longer matches. Strict per-hop evaluation — correct
    per RFC 9309 — therefore refuses an endpoint the publisher plainly opened.

    The exception is deliberately awkward: it is per-host, per-path-prefix, and
    invalid without a written reason, so it lives in suppliers.json where anyone
    can see which supplier got an exception and why. That is the difference between
    an auditable exception and quietly deciding we know better than the file.

        "robots_override": {
          "allow_paths": ["/api/listings"],
          "reason": "Allow: /api/listings/ grants this endpoint; their 308 to the
                     slash-less canonical form is what the Disallow catches.
                     Reviewed 2026-07-20."
        }
    """

    allow_paths: tuple[str, ...]
    reason: str
    hosts: tuple[str, ...]

    @classmethod
    def from_entry(cls, entry: dict) -> Override | None:
        raw = (entry or {}).get("robots_override")
        if not raw:
            return None
        paths = tuple(p for p in (raw.get("allow_paths") or []) if p)
        reason = (raw.get("reason") or "").strip()
        if not paths:
            return None
        if not reason:
            # Fail loudly. An undocumented override is the thing this design exists
            # to prevent, so it must not be possible to add one by omission.
            raise ValueError(
                f"robots_override for {entry.get('host')!r} needs a 'reason' "
                "recording who reviewed the robots.txt and why the exception holds")
        hosts = tuple(h.lower() for h in (raw.get("hosts") or [entry.get("host", "")]) if h)
        return cls(paths, reason, hosts)

    def covers(self, url: str) -> bool:
        parts = urlsplit(url if "//" in url else f"https://{url}")
        host = parts.netloc.lower().split(":")[0]
        if not any(host == h or host.endswith("." + h) for h in self.hosts):
            return False
        path = parts.path or "/"
        return any(path.startswith(p) for p in self.allow_paths)


def origin_of(url: str) -> str:
    parts = urlsplit(url if "//" in url else f"https://{url}")
    scheme = parts.scheme or "https"
    return f"{scheme}://{parts.netloc}"


def _path_of(url: str) -> str:
    parts = urlsplit(url if "//" in url else f"https://{url}")
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def looks_like_html(text: str) -> bool:
    """Is this an HTML page rather than a robots.txt?

    Single-page-app hosts (every Stone Profits tenant, slabcloud.com) answer
    /robots.txt with their catch-all index.html at status 200. Parsing that as a
    rules file yields "zero rules, everything allowed" — the right answer for the
    wrong reason, and one that would start silently inventing rules the day a
    marketing page happens to contain a line like "Disallow: nothing". Treat it as
    what it is: no robots.txt published.
    """
    head = text.lstrip()[:200].lower()
    return head.startswith(("<!doctype", "<html", "<?xml")) or "<head>" in head


# The directives a real robots.txt is made of. An empty file IS a valid robots.txt meaning
# "no rules", so emptiness passes; a non-empty body with none of these is something else
# wearing text/plain.
_ROBOTS_DIRECTIVE = re.compile(
    r"(?im)^\s*(user-agent|allow|disallow|sitemap|crawl-delay|host)\s*:")


def looks_like_robots(text: str) -> bool:
    """Does this 200 body actually look like a rules file?

    `looks_like_html` catches the SPA case; this catches everything else that answers
    /robots.txt with something that is not one — a health-check string, a JSON error, an API
    banner. Without it, `parse()` reads zero rules and reports "allowed", which is the right
    answer for the wrong reason and starts inventing rules the day such a body contains a
    line with a colon in it.
    """
    body = (text or "").strip()
    if not body:
        return True                      # an empty robots.txt is a real, valid robots.txt
    if body.lstrip().startswith(("{", "[")):
        return False                     # JSON, not a rules file
    return bool(_ROBOTS_DIRECTIVE.search(body))


def _policy_from_response(status: int, text: str, agent: str,
                          headers: dict | None = None) -> RobotsPolicy:
    if 200 <= status < 300:
        if looks_like_html(text):
            return RobotsPolicy.none_published(f"HTTP {status} but HTML, not robots.txt")
        if not looks_like_robots(text):
            # A 200 that is neither HTML nor a rules file. Measured:
            # api.vavastone.com/robots.txt answers 200 text/plain with the body
            # "Connected to backend!". looks_like_html misses it, so parse() saw zero
            # colon-bearing lines and returned an empty ruleset — "published a file with no
            # rules" rather than "published no file". Same right-answer-wrong-reason trap
            # the SPA case above already guards, and one bad marketing line away from
            # inventing a rule.
            return RobotsPolicy.none_published(f"HTTP {status} but not a robots.txt")
        return RobotsPolicy.parse(text, agent)
    # A CHALLENGE is not an absent file. RFC 9309 2.3.1.3 says an "unavailable" robots.txt
    # means no restrictions, and this used to apply that to any 4xx — the old comment named
    # a Cloudflare 403 challenge explicitly as a case it covered. That was defensible only
    # while we could not tell the two apart. We can now, and they are opposite statements:
    # a 404 is "there is no file here", a challenge is "you specifically are not welcome".
    # Reading the second as permission inverts the supplier's answer on the very fetch whose
    # job is to ask their permission.
    if detect_challenge(status, headers or {}, text):
        return RobotsPolicy.unreachable(f"HTTP {status} bot-protection challenge")
    if 400 <= status < 500:
        # A genuine "unavailable" — the RFC reading, for the case it actually describes.
        return RobotsPolicy.none_published(f"HTTP {status}")
    return RobotsPolicy.unreachable(f"HTTP {status}")


# --- caches --------------------------------------------------------------------

class RobotsCache:
    """One robots.txt fetch per origin per run, shared by every caller.

    A crawl touches an origin hundreds of times (every product page on a
    genericfeed site); re-fetching robots.txt each time would be both slow and
    rude. Concurrent callers for the same origin await one fetch rather than
    racing to duplicate it.
    """

    def __init__(self, agent: str = AGENT_TOKEN, *, client: httpx.AsyncClient | None = None,
                 overrides: list[Override] | None = None) -> None:
        self.agent = agent
        self._client = client
        self._policies: dict[str, RobotsPolicy] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Each Override is host-scoped, so a cache shared across a whole run cannot
        # leak one supplier's reviewed exception onto another supplier's host.
        self.overrides = list(overrides or [])
        self.overridden: list[tuple[str, Override]] = []

    async def _fetch(self, origin: str) -> RobotsPolicy:
        client = self._client
        close = False
        if client is None:
            client = httpx.AsyncClient(headers={"User-Agent": USER_AGENT},
                                       follow_redirects=True, timeout=FETCH_TIMEOUT)
            close = True
        try:
            r = await client.get(f"{origin}/robots.txt", timeout=FETCH_TIMEOUT,
                                 extensions={_ROBOTS_FETCH: True})
            return _policy_from_response(r.status_code, r.text, self.agent,
                                        dict(r.headers))
        except Exception as e:  # noqa: BLE001 - any failure to ask means we don't crawl
            return RobotsPolicy.unreachable(type(e).__name__)
        finally:
            if close:
                await client.aclose()

    async def policy(self, url: str) -> RobotsPolicy:
        origin = origin_of(url)
        if origin in self._policies:
            return self._policies[origin]
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._policies:  # another waiter may have filled it
                self._policies[origin] = await self._fetch(origin)
        return self._policies[origin]

    async def check(self, url: str) -> Decision:
        # Fetching robots.txt itself must never require permission from robots.txt.
        if _path_of(url).split("?")[0] == "/robots.txt":
            return Decision(True, ALLOWED)
        policy = await self.policy(url)
        decision = policy.allows(_path_of(url))
        # An override only ever rescues an explicit BLOCKED. It must not paper over
        # UNREACHABLE — "we could not ask" is never something a human pre-approved.
        if decision.reason == BLOCKED:
            for ov in self.overrides:
                if ov.covers(url):
                    self.overridden.append((url, ov))
                    return Decision(True, ALLOWED, f"override: {ov.reason}",
                                    crawl_delay=decision.crawl_delay)
        return decision

    async def allowed(self, url: str) -> bool:
        return (await self.check(url)).allowed


class SyncRobotsCache:
    """Blocking twin of `RobotsCache` for the synchronous discovery pass."""

    def __init__(self, agent: str = AGENT_TOKEN, *, client: httpx.Client | None = None) -> None:
        self.agent = agent
        self._client = client
        self._policies: dict[str, RobotsPolicy] = {}
        self._lock = threading.Lock()

    def _fetch(self, origin: str) -> RobotsPolicy:
        client = self._client
        close = False
        if client is None:
            client = httpx.Client(headers={"User-Agent": USER_AGENT},
                                  follow_redirects=True, timeout=FETCH_TIMEOUT)
            close = True
        try:
            r = client.get(f"{origin}/robots.txt", timeout=FETCH_TIMEOUT,
                           extensions={_ROBOTS_FETCH: True})
            return _policy_from_response(r.status_code, r.text, self.agent)
        except Exception as e:  # noqa: BLE001
            return RobotsPolicy.unreachable(type(e).__name__)
        finally:
            if close:
                client.close()

    def policy(self, url: str) -> RobotsPolicy:
        origin = origin_of(url)
        with self._lock:
            if origin not in self._policies:
                self._policies[origin] = self._fetch(origin)
            return self._policies[origin]

    def check(self, url: str) -> Decision:
        if _path_of(url).split("?")[0] == "/robots.txt":
            return Decision(True, ALLOWED)
        return self.policy(url).allows(_path_of(url))

    def allowed(self, url: str) -> bool:
        return self.check(url).allowed


# --- the client providers use ---------------------------------------------------

class PoliteClient:
    """`httpx.AsyncClient` that will not fetch what robots.txt forbids.

    Drop-in for the providers: same constructor kwargs, same `get`/`post`, same
    `async with`. Swapping `httpx.AsyncClient(` for `robots.PoliteClient(` is the
    entire change a provider needs, and every request it makes — including ones
    to third-party origins it wanders onto — is checked against that origin's own
    rules.
    """

    def __init__(self, *, robots: RobotsCache | None = None, agent: str = AGENT_TOKEN,
                 override: Override | None = None,
                 headers: dict[str, str] | None = None,
                 event_hooks: dict[str, list] | None = None, **kwargs) -> None:
        headers = dict(headers or {})
        headers.setdefault("User-Agent", USER_AGENT)
        # The check is a request event hook rather than a wrapper around .get(),
        # because httpx follows redirects internally: a wrapper sees the URL we
        # asked for and never the one we end up fetching, so a 30x into a
        # disallowed path (or onto another origin entirely) would sail through.
        # The hook fires once per hop, so every URL actually requested is checked.
        hooks: dict[str, list] = {"request": [], "response": []}
        for key, fns in (event_hooks or {}).items():
            hooks.setdefault(key, []).extend(fns)
        hooks["request"].insert(0, self._guard_request)
        # The challenge check is a RESPONSE hook, in the same shared layer and for the same
        # reason: it belongs wherever the fetch actually happens, so every provider inherits
        # it and none has to remember. Header-only — the response is unread at this point and
        # reading it here would break client.stream() everywhere.
        hooks["response"].insert(0, self._guard_response)
        self._client = httpx.AsyncClient(headers=headers, event_hooks=hooks, **kwargs)
        # robots.txt is fetched with the same client so a provider's TLS quirks
        # (UMI's legacy-cipher context, say) apply to the check as well — otherwise
        # the check would fail "unreachable" on exactly the hosts that need it.
        self.robots = robots or RobotsCache(
            agent, client=self._client, overrides=[override] if override else None)
        self.blocked: list[tuple[str, Decision]] = []
        self.challenged: list[tuple[str, str]] = []

    async def _guard_request(self, request: httpx.Request) -> None:
        # The robots.txt fetch itself is exempt. Without this, a robots.txt that
        # redirects to a normal page would re-enter the cache for an origin whose
        # lock this very call is holding, and deadlock.
        if request.extensions.get(_ROBOTS_FETCH):
            return
        url = str(request.url)
        decision = await self.robots.check(url)
        if not decision.allowed:
            self.blocked.append((url, decision))
            raise Disallowed(url, decision)

    async def _guard_response(self, response: httpx.Response) -> None:
        """Turn a bot-protection challenge into a decision we record, not a page we parse.

        Deliberately does NOT read `response`: httpx fires this hook before the body is
        loaded, so touching `.text` here would force a read and break streaming for every
        caller. Headers carry the decisive evidence anyway (`cf-mitigated: challenge`).

        robots.txt fetches are exempt, exactly as in the request hook — a challenge on the
        policy file itself is the RobotsCache's business (it reads as UNREACHABLE, which is
        retryable) and must not be reclassified here.
        """
        if response.request.extensions.get(_ROBOTS_FETCH):
            return
        # Headers first, and for almost every response that is the whole check — the hook
        # fires on an UNREAD response and forcing a read would break client.stream().
        #
        # The body is read for refusal statuses only (403/503). That exception is what makes
        # the body rule reachable at all: it was written in AIL-33 and then dead, because the
        # hook never passed a body, so only Cloudflare's own `cf-mitigated` header stopped
        # anything. Reading here is safe precisely because nothing legitimate streams from a
        # refusal — there is no download to disturb, and the body is an error page.
        body = ""
        if response.status_code in CHALLENGE_STATUSES:
            try:
                body = (await response.aread()).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - unreadable body just means header-only
                body = ""
        reason = detect_challenge(response.status_code, response.headers, body)
        if reason:
            url = str(response.request.url)
            self.challenged.append((url, reason))
            raise Challenged(url, reason)

    async def __aenter__(self) -> PoliteClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.__aexit__(*exc)

    async def aclose(self) -> None:
        await self._client.aclose()

    def __getattr__(self, name: str):
        # Everything else — get, post, stream, headers, cookies — is the underlying
        # client's, and is gated by the request hook wherever it ends up fetching.
        return getattr(self._client, name)

def client_for(entry: dict, **kwargs) -> PoliteClient:
    """The client a provider should use: gated, and carrying that supplier's
    reviewed robots exception if suppliers.json records one."""
    return PoliteClient(override=Override.from_entry(entry), **kwargs)
