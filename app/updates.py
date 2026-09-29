"""Is there a newer DeadAir? Checked against GitHub's public API, at most daily.

What "newer" means depends on how the running image was built:
  - a release (APP_VERSION "v1.2.3"): a later GitHub release exists
  - main (APP_VERSION "main"):         main has commits this build doesn't
  - anything else (pr-N, dev):         not checked; it's a test build
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

GITHUB_REPO = "JamesHeadrick/DeadAir-TV"
API = f"https://api.github.com/repos/{GITHUB_REPO}"
CHECK_EVERY_S = 86400
RETRY_AFTER_S = 3600  # after a failed check
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "DeadAir (+https://github.com/JamesHeadrick/DeadAir-TV)",
}

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_semver(version: str | None) -> tuple[int, int, int] | None:
    m = _SEMVER.match(version or "")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


async def check(version: str, commit: str | None, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """What's newer than this build, or None if it isn't a build we can check.

    Returns {"available": bool, "latest": str | None, "url": str | None, "behind": int | None}.
    Raises httpx.HTTPError if GitHub can't be reached.
    """
    current = parse_semver(version)
    if not current and not (version == "main" and commit):
        return None
    async with httpx.AsyncClient(timeout=15, headers=HEADERS, transport=transport) as client:
        if current:
            r = await client.get(f"{API}/releases/latest")
            if r.status_code == 404:  # no releases published yet
                return {"available": False, "latest": None, "url": None, "behind": None}
            r.raise_for_status()
            release = r.json()
            latest = parse_semver(release.get("tag_name"))
            return {
                "available": bool(latest and latest > current),
                "latest": release.get("tag_name"),
                "url": release.get("html_url"),
                "behind": None,
            }
        r = await client.get(f"{API}/compare/{commit}...main")
        r.raise_for_status()
        behind = int(r.json().get("ahead_by") or 0)  # commits on main that this build lacks
        return {
            "available": behind > 0,
            "latest": "main",
            "url": f"https://github.com/{GITHUB_REPO}/compare/{commit[:7]}...main",
            "behind": behind,
        }


class UpdateChecker:
    """Caches check() so GitHub is asked at most once a day (hourly after a failure)."""

    def __init__(self, version: str, commit: str | None, transport: httpx.AsyncBaseTransport | None = None):
        self.version = version
        self.commit = commit
        self.transport = transport  # for tests
        self._result: dict | None = None
        self._checked_at = 0.0
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> dict:
        async with self._lock:
            if time.time() >= self._next_at:
                self._checked_at = time.time()
                try:
                    self._result = await check(self.version, self.commit, self.transport)
                    self._next_at = self._checked_at + CHECK_EVERY_S
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("update check failed: %s", e)
                    self._next_at = self._checked_at + RETRY_AFTER_S  # keep the last good result
            return {"update": self._result, "checked_at": self._checked_at or None}
