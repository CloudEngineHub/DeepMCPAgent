"""Generate the GitHub social preview PNG from social-preview.html.

Usage:
    pip install playwright
    playwright install chromium
    python scripts/generate-social-preview.py

Outputs: docs/assets/social-preview.png (1280x640, ready to upload to
GitHub repo settings -> Social preview).
"""

from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "docs" / "assets" / "social-preview.html"
OUT = ROOT / "docs" / "assets" / "social-preview.png"


def main() -> None:
    if not HTML.exists():
        raise FileNotFoundError(f"Source HTML not found: {HTML}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 800}, device_scale_factor=2)
        page = context.new_page()
        page.goto(HTML.as_uri())
        page.wait_for_load_state("networkidle")
        card = page.locator(".card").first
        card.screenshot(path=str(OUT), omit_background=False)
        browser.close()

    print(f"Wrote {OUT}")
    print("Upload at: https://github.com/promptise-com/foundry/settings -> Social preview")


if __name__ == "__main__":
    main()
