"""Live check of the Wikidata show-link lookup against well-known shows.

    python -m tools.wikidata_check

Prints the links found and exits non-zero if the query fails or finds
nothing at all (which would mean the query or Wikidata's data model broke).
"""

from __future__ import annotations

import asyncio
import sys

from app import wikidata

SHOWS = {
    1400: "Seinfeld",
    2316: "The Office (US)",
    615: "Futurama",
    1396: "Breaking Bad",
    1399: "Game of Thrones",
    66732: "Stranger Things",
    82856: "The Mandalorian",
    97546: "Ted Lasso",
    100088: "The Last of Us",
    107113: "Only Murders in the Building",
}


def main() -> int:
    links = asyncio.run(wikidata.fetch_show_links(list(SHOWS)))
    for tmdb_id, title in SHOWS.items():
        found = links.get(tmdb_id, {})
        print(f"{title} ({tmdb_id}):")
        for service, url in sorted(found.items()):
            print(f"    {service:20} {url}")
        if not found:
            print("    (nothing)")
    total = sum(len(v) for v in links.values())
    print(f"\n{total} links for {len(links)} of {len(SHOWS)} shows")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
