from pathlib import Path
from urllib.parse import urljoin
import csv
import re
import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


INPUT_FILE = "product_urls.txt"
IMAGE_OUTPUT_FILE = "image_links.txt"
CSV_OUTPUT_FILE = "results.csv"

PAGE_TIMEOUT = 30_000
SELECTOR_TIMEOUT = 10_000


def clean_text(text: str) -> str:
    """Normalize whitespace in extracted text."""
    return re.sub(r"\s+", " ", text or "").strip()


def extract_image_url(locator, page_url: str) -> str | None:
    """
    Extract image URL from an element.

    Supports:
    - img[src]
    - img[data-src]
    - img[data-lazy-src]
    - img[data-original]
    - img[srcset]
    - background-image
    - image container containing an img
    """

    try:
        # If selector points directly to an img, use it.
        tag_name = locator.evaluate("(el) => el.tagName.toLowerCase()")

        if tag_name == "img":
            element = locator
        else:
            # Otherwise search inside the selected container.
            element = locator.locator("img").first

            if element.count() == 0:
                element = None

        if element:
            attributes = [
                "src",
                "data-src",
                "data-lazy-src",
                "data-original",
                "data-image",
            ]

            for attr in attributes:
                value = element.get_attribute(attr)

                if value and value.strip():
                    return urljoin(page_url, value.strip())

            # srcset
            srcset = element.get_attribute("srcset")

            if srcset:
                candidates = []

                for item in srcset.split(","):
                    item = item.strip()

                    if not item:
                        continue

                    parts = item.split()

                    if parts:
                        url = parts[0]
                        descriptor = parts[1] if len(parts) > 1 else ""

                        score = 0

                        match = re.search(r"(\d+)w", descriptor)
                        if match:
                            score = int(match.group(1))

                        candidates.append((score, url))

                if candidates:
                    # Use highest resolution image.
                    candidates.sort(reverse=True)

                    return urljoin(page_url, candidates[0][1])

        # Background image support
        style = locator.evaluate(
            """
            (el) => {
                const style = window.getComputedStyle(el);
                return style.backgroundImage || "";
            }
            """
        )

        if style and style != "none":
            match = re.search(r'url\(["\']?(.*?)["\']?\)', style)

            if match:
                return urljoin(page_url, match.group(1))

    except Exception:
        return None

    return None


def extract_title(locator) -> str | None:
    """Extract and normalize product title."""
    try:
        text = locator.inner_text()

        text = clean_text(text)

        return text if text else None

    except Exception:
        return None


def read_urls() -> list[str]:
    """Read product URLs from input file."""
    path = Path(INPUT_FILE)

    if not path.exists():
        print(f"ERROR: {INPUT_FILE} not found.")
        sys.exit(1)

    urls = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        urls.append(line)

    return urls


def main():
    print("=" * 60)
    print("Product Image Scraper")
    print("=" * 60)

    # ---------------------------------------------------------
    # Ask user for selectors
    # ---------------------------------------------------------

    print("\nEnter CSS selector for the PRODUCT IMAGE")
    print("It can point directly to <img> or to its container.")
    image_selector = input("Image selector: ").strip()

    if not image_selector:
        print("ERROR: Image selector cannot be empty.")
        sys.exit(1)

    print("\nEnter CSS selector for the PRODUCT TITLE")
    title_selector = input("Title selector: ").strip()

    if not title_selector:
        print("ERROR: Title selector cannot be empty.")
        sys.exit(1)

    # ---------------------------------------------------------
    # Read URLs
    # ---------------------------------------------------------

    urls = read_urls()

    if not urls:
        print(f"ERROR: No URLs found in {INPUT_FILE}")
        sys.exit(1)

    print(f"\nFound {len(urls)} product URLs.")

    # ---------------------------------------------------------
    # Prepare output files
    # ---------------------------------------------------------

    image_urls = []
    results = []

    # ---------------------------------------------------------
    # Start Playwright
    # ---------------------------------------------------------

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        for index, url in enumerate(urls, start=1):

            print("\n" + "-" * 60)
            print(f"[{index}/{len(urls)}]")
            print(url)

            result = {
                "product_url": url,
                "title": "",
                "image_url": "",
                "status": "",
                "error": "",
            }

            try:
                # -------------------------------------------------
                # Open product page
                # -------------------------------------------------

                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT,
                )

                # Give JS-rendered content a moment to settle.
                page.wait_for_timeout(1000)

                # Scroll page to trigger lazy-loaded images.
                page.evaluate(
                    """
                    async () => {
                        window.scrollTo(0, document.body.scrollHeight);
                        await new Promise(r => setTimeout(r, 500));
                        window.scrollTo(0, 0);
                    }
                    """
                )

                # -------------------------------------------------
                # Find title
                # -------------------------------------------------

                try:
                    title_locator = page.locator(title_selector).first

                    title_locator.wait_for(
                        state="visible",
                        timeout=SELECTOR_TIMEOUT,
                    )

                    title = extract_title(title_locator)

                except PlaywrightTimeoutError:
                    title = None

                # -------------------------------------------------
                # Find image
                # -------------------------------------------------

                try:
                    image_locator = page.locator(image_selector).first

                    image_locator.wait_for(
                        state="attached",
                        timeout=SELECTOR_TIMEOUT,
                    )

                    image_url = extract_image_url(
                        image_locator,
                        url,
                    )

                except PlaywrightTimeoutError:
                    image_url = None

                # -------------------------------------------------
                # Validate result
                # -------------------------------------------------

                result["title"] = title or ""
                result["image_url"] = image_url or ""

                if image_url:

                    result["status"] = "success"

                    image_urls.append(image_url)

                    print(f"✓ Title: {title or 'NOT FOUND'}")
                    print(f"✓ Image: {image_url}")

                else:

                    result["status"] = "image_not_found"

                    print("✗ Image not found")

                    if title:
                        print(f"✓ Title: {title}")

            except PlaywrightTimeoutError:

                result["status"] = "timeout"
                result["error"] = "Page loading timeout"

                print("✗ Timeout")

            except Exception as e:

                result["status"] = "error"
                result["error"] = str(e)

                print(f"✗ Error: {e}")

            results.append(result)

            # Small delay between requests.
            time.sleep(0.5)

        browser.close()

    # ---------------------------------------------------------
    # Save image URLs
    # ---------------------------------------------------------

    # Remove duplicates while preserving order.
    unique_image_urls = list(dict.fromkeys(image_urls))

    Path(IMAGE_OUTPUT_FILE).write_text(
        "\n".join(unique_image_urls),
        encoding="utf-8",
    )

    # ---------------------------------------------------------
    # Save detailed CSV
    # ---------------------------------------------------------

    with open(
        CSV_OUTPUT_FILE,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=[
                "product_url",
                "title",
                "image_url",
                "status",
                "error",
            ],
        )

        writer.writeheader()
        writer.writerows(results)

    # ---------------------------------------------------------
    # Summary
    # ---------------------------------------------------------

    success_count = sum(
        1 for item in results
        if item["status"] == "success"
    )

    failed_count = len(results) - success_count

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)

    print(f"Products:       {len(urls)}")
    print(f"Images found:   {success_count}")
    print(f"Failed:         {failed_count}")
    print(f"Unique images:  {len(unique_image_urls)}")

    print("\nOutput files:")
    print(f"  {IMAGE_OUTPUT_FILE}")
    print(f"  {CSV_OUTPUT_FILE}")


if __name__ == "__main__":
    main()