"""Render the home-screen icon PNGs from tools/icon-full.svg.

Run after changing the icon (needs Playwright's Chromium):
    python tools/make_icons.py
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tools" / "icon-full.svg"
OUT = ROOT / "app" / "static"
SIZES = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}


def main() -> None:
    svg = SOURCE.read_text()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, size in SIZES.items():
            page = browser.new_page(viewport={"width": size, "height": size})
            page.set_content(
                f"<style>html,body{{margin:0}}svg{{display:block;width:{size}px;height:{size}px}}</style>{svg}"
            )
            page.screenshot(path=str(OUT / name))
            page.close()
            print(f"wrote {name} ({size}px)")
        browser.close()


if __name__ == "__main__":
    main()
