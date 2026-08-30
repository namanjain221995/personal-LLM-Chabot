"""robots.txt for the site crawler (2026-08-30).

Parsing is stdlib ``urllib.robotparser`` — but the FETCH goes through
``net.safe_fetch``, never ``RobotFileParser.read()``: read() would open the
URL itself, bypassing the SSRF guard (private-IP refusal, DNS-rebinding
check, size cap) and announcing Python-urllib's default User-Agent instead
of ours.

Status semantics follow RFC 9309: a 4xx robots.txt means "no rules — crawl
allowed"; a 5xx (or unreachable host) means the site could not state its
rules, so the polite reading is "assume disallowed" and the crawl declines.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from . import net

log = logging.getLogger(__name__)

#: The honest identity this crawler announces. A local personal assistant is
#: not a stealth scraper; sites that refuse bots get to refuse this one.
USER_AGENT = "TechSaraBot/1.0 (+local personal AI assistant)"

_SITEMAP_RE = re.compile(r"(?im)^\s*sitemap\s*:\s*(\S+)")

#: robots.txt files are small; anything bigger is not a rules file.
_MAX_ROBOTS_BYTES = 512 * 1024


@dataclass
class RobotRules:
    allowed_all: bool = False
    #: When the crawl must not proceed at all (5xx robots, unreachable host).
    declined: bool = False
    decline_reason: str = ""
    sitemaps: List[str] = field(default_factory=list)
    crawl_delay_s: float = 0.0
    _parser: RobotFileParser | None = None

    def allows(self, url: str) -> bool:
        if self.declined:
            return False
        if self.allowed_all or self._parser is None:
            return True
        try:
            return self._parser.can_fetch(USER_AGENT, url)
        except Exception:  # noqa: BLE001 — a broken rules file blocks nothing
            return True


async def fetch_rules(root_url: str) -> RobotRules:
    """Rules for the host of `root_url`, fetched safely, parsed with stdlib."""
    parts = urlparse(root_url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        fetched = await net.safe_fetch(
            robots_url,
            timeout_ms=8000,
            max_bytes=_MAX_ROBOTS_BYTES,
            accept="text/plain",
        )
    except net.FetchError as exc:
        status = getattr(exc, "status", None)
        if status is not None and 400 <= status < 500:
            # RFC 9309: no robots file = no restrictions.
            return RobotRules(allowed_all=True)
        return RobotRules(
            declined=True,
            decline_reason=f"robots.txt could not be read ({exc})",
        )
    except Exception as exc:  # noqa: BLE001 — includes UnsafeURLError
        return RobotRules(declined=True, decline_reason=str(exc))

    text = fetched.body.decode("utf-8", errors="replace")
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    delay = 0.0
    try:
        raw = parser.crawl_delay(USER_AGENT)
        if raw:
            delay = min(float(raw), 10.0)  # a 60 s delay is a "no", capped
    except Exception:  # noqa: BLE001
        delay = 0.0
    sitemaps = [m.group(1).strip() for m in _SITEMAP_RE.finditer(text)]
    return RobotRules(
        allowed_all=False,
        sitemaps=sitemaps[:10],
        crawl_delay_s=delay,
        _parser=parser,
    )
