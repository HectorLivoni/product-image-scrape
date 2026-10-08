from pathlib import Path
from urllib.parse import urljoin, urlparse
import csv
import mimetypes
import re
import sys
import time

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# Configuration
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

INPUT_CSV = BASE_DIR / "products.csv"

# Create a separate folder inside images based on the script name.
SCRIPT_NAME = Path(__file__).stem
OUTPUT_DIR = BASE_DIR / "images" / SCRIPT_NAME

PAGE_TIMEOUT = 30_000
SELECTOR_TIMEOUT = 10_000
REQUEST_TIMEOUT = 30_000


# ============================================================
# Helpers
# ============================================================

def clean_filename(name: str) -> str:
    """
    Convert product name into a safe filename.

    Keeps Persian characters but removes characters that are
    problematic on Windows/Linux/macOS.
    """
    name = (name or "").strip()

    # Replace forbidden filename characters.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "-", name)

    # Collapse multiple spaces.
    name = re.sub(r"\s+", " ", name)

    # Remove trailing dots/spaces.
    name = name.rstrip(". ")

    # Avoid empty filename.
    return name or "product"


def read_products():
    """Read Input Product + Product URL from CSV."""

    if not INPUT_CSV.exists():
        print(f"ERROR: CSV file not found:")
        print(f"       {INPUT_CSV}")
        sys.exit(1)

    products = []

    with INPUT_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = csv.DictReader(file)

        if reader.fieldnames is None:
            print("ERROR: CSV has no header.")
            sys.exit(1)

        required_columns = {
            "Input Product",
            "Product URL",
        }

        missing = required_columns - set(reader.fieldnames)

        if missing:
            print(
                "ERROR: Missing required CSV columns: "
                + ", ".join(sorted(missing))
            )
            print("Required columns:")
            print("Input Product,Product URL")
            sys.exit(1)

        for row_number, row in enumerate(reader, start=2):

            product_name = (row.get("Input Product") or "").strip()
            product_url = (row.get("Product URL") or "").strip()

            if not product_name and not product_url:
                continue

            if not product_name:
                print(
                    f"WARNING: Row {row_number}: "
                    f"Input Product is empty. Skipping."
                )
                continue

            if not product_url:
                print(
                    f"WARNING: Row {row_number}: "
                    f"Product URL is empty. Skipping."
                )
                continue

            products.append(
                {
                    "input_product": product_name,
                    "product_url": product_url,
                    "row_number": row_number,
                }
            )

    return products


def get_image_url(locator, page_url: str):
    """
    Extract the best available image URL from the selected element.

    The selector can point directly to <img> or to a container
    containing an <img>.
    """

    try:
        tag_name = locator.evaluate(
            "(el) => el.tagName.toLowerCase()"
        )

        if tag_name == "img":
            image = locator
        else:
            image = locator.locator("img").first

            if image.count() == 0:
                image = None

        if image:
            # Try the actual browser-resolved currentSrc first.
            try:
                current_src = image.evaluate(
                    "(img) => img.currentSrc || ''"
                )

                if current_src:
                    return urljoin(page_url, current_src)
            except Exception:
                pass

            # Common lazy-loading attributes.
            attributes = [
                "src",
                "data-src",
                "data-lazy-src",
                "data-original",
                "data-image",
                "data-url",
            ]

            for attribute in attributes:
                try:
                    value = image.get_attribute(attribute)

                    if value and value.strip():
                        return urljoin(
                            page_url,
                            value.strip(),
                        )
                except Exception:
                    pass

            # srcset fallback.
            try:
                srcset = image.get_attribute("srcset")

                if srcset:
                    candidates = []

                    for item in srcset.split(","):
                        item = item.strip()

                        if not item:
                            continue

                        parts = item.split()

                        image_url = parts[0]
                        descriptor = parts[1] if len(parts) > 1 else ""

                        width = 0

                        match = re.search(
                            r"(\d+)w",
                            descriptor,
                        )

                        if match:
                            width = int(match.group(1))

                        candidates.append(
                            (width, image_url)
                        )

                    if candidates:
                        candidates.sort(
                            key=lambda x: x[0],
                            reverse=True,
                        )

                        return urljoin(
                            page_url,
                            candidates[0][1],
                        )
            except Exception:
                pass

        # Try CSS background-image.
        try:
            background = locator.evaluate(
                """
                (el) => window.getComputedStyle(el).backgroundImage
                """
            )

            if background and background != "none":

                match = re.search(
                    r'url\(["\']?(.*?)["\']?\)',
                    background,
                )

                if match:
                    return urljoin(
                        page_url,
                        match.group(1),
                    )
        except Exception:
            pass

    except Exception:
        pass

    return None


def extension_from_content_type(content_type: str):
    """Get file extension from HTTP content type."""

    if not content_type:
        return None

    content_type = content_type.lower().split(";")[0].strip()

    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/svg+xml": ".svg",
    }

    return mapping.get(content_type)


def extension_from_url(image_url: str):
    """Try to detect image extension from URL."""

    try:
        path = urlparse(image_url).path

        suffix = Path(path).suffix.lower()

        allowed = {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif",
            ".avif",
            ".bmp",
            ".tiff",
            ".svg",
        }

        if suffix in allowed:
            return ".jpg" if suffix == ".jpeg" else suffix

    except Exception:
        pass

    return None


def create_output_filename(product_name: str, extension: str):
    """Create final image filename."""

    safe_name = clean_filename(product_name)

    return OUTPUT_DIR / f"{safe_name}{extension}"


def make_unique_path(path: Path):
    """
    If a filename already exists, add _2, _3, ...
    """

    if not path.exists():
        return path

    counter = 2

    while True:

        candidate = path.with_name(
            f"{path.stem}_{counter}{path.suffix}"
        )

        if not candidate.exists():
            return candidate

        counter += 1


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 65)
    print("Product Image Downloader")
    print("=" * 65)

    # --------------------------------------------------------
    # Ask for CSS selector
    # --------------------------------------------------------

    print()
    print("Enter CSS selector for the PRODUCT IMAGE.")
    print()
    print("Examples:")
    print("  .product-gallery img")
    print("  .product-image")
    print("  div.main-image")
    print()

    image_selector = input(
        "Image selector: "
    ).strip()

    if not image_selector:
        print("ERROR: Image selector cannot be empty.")
        sys.exit(1)

    # --------------------------------------------------------
    # Read CSV
    # --------------------------------------------------------

    products = read_products()

    if not products:
        print("ERROR: No valid products found in CSV.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print(f"Products found: {len(products)}")
    print(f"Output folder:  {OUTPUT_DIR}")
    print()

    results = []

    # --------------------------------------------------------
    # Start Playwright
    # --------------------------------------------------------

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
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

        # ----------------------------------------------------
        # Process each product
        # ----------------------------------------------------

        for index, product in enumerate(
            products,
            start=1,
        ):

            product_name = product["input_product"]
            product_url = product["product_url"]

            print()
            print("-" * 65)
            print(
                f"[{index}/{len(products)}] "
                f"{product_name}"
            )
            print(product_url)

            result = {
                "input_product": product_name,
                "product_url": product_url,
                "image_url": "",
                "saved_file": "",
                "status": "",
                "error": "",
            }

            try:
                # ------------------------------------------------
                # Open product page
                # ------------------------------------------------

                response = page.goto(
                    product_url,
                    wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT,
                )

                page.wait_for_timeout(1000)

                # ------------------------------------------------
                # Trigger lazy-loaded images
                # ------------------------------------------------

                try:
                    page.locator(
                        image_selector
                    ).first.scroll_into_view_if_needed(
                        timeout=SELECTOR_TIMEOUT
                    )
                except Exception:
                    pass

                page.wait_for_timeout(1000)

                # ------------------------------------------------
                # Find image element/container
                # ------------------------------------------------

                image_locator = page.locator(
                    image_selector
                ).first

                image_locator.wait_for(
                    state="attached",
                    timeout=SELECTOR_TIMEOUT,
                )

                image_url = get_image_url(
                    image_locator,
                    product_url,
                )

                if not image_url:
                    raise RuntimeError(
                        "Could not extract image URL."
                    )

                result["image_url"] = image_url

                print(f"Image URL: {image_url}")

                # ------------------------------------------------
                # Download image
                # ------------------------------------------------

                image_response = context.request.get(
                    image_url,
                    timeout=REQUEST_TIMEOUT,
                    fail_on_status_code=False,
                )

                if not image_response.ok:

                    raise RuntimeError(
                        f"Image download failed "
                        f"(HTTP {image_response.status})."
                    )

                content_type = image_response.headers.get(
                    "content-type",
                    "",
                )

                # ------------------------------------------------
                # Determine extension
                # ------------------------------------------------

                extension = (
                    extension_from_content_type(
                        content_type
                    )
                    or extension_from_url(
                        image_url
                    )
                    or ".jpg"
                )

                # ------------------------------------------------
                # Build filename
                # ------------------------------------------------

                output_path = create_output_filename(
                    product_name,
                    extension,
                )

                output_path = make_unique_path(
                    output_path
                )

                # ------------------------------------------------
                # Save file
                # ------------------------------------------------

                output_path.write_bytes(
                    image_response.body()
                )

                result["saved_file"] = str(
                    output_path.relative_to(BASE_DIR)
                )

                result["status"] = "success"

                print(
                    f"✓ Saved: {result['saved_file']}"
                )

            except PlaywrightTimeoutError:

                result["status"] = "timeout"
                result["error"] = (
                    "Page or image selector timeout."
                )

                print("✗ Timeout")

            except Exception as error:

                result["status"] = "error"
                result["error"] = str(error)

                print(f"✗ Error: {error}")

            results.append(result)

            # Be polite to the target server.
            time.sleep(0.5)

        browser.close()

    # ----------------------------------------------------------
    # Save processing report
    # ----------------------------------------------------------

    report_path = BASE_DIR / "download_report.csv"

    with report_path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=[
                "input_product",
                "product_url",
                "image_url",
                "saved_file",
                "status",
                "error",
            ],
        )

        writer.writeheader()
        writer.writerows(results)

    # ----------------------------------------------------------
    # Summary
    # ----------------------------------------------------------

    success_count = sum(
        1
        for result in results
        if result["status"] == "success"
    )

    failed_count = len(results) - success_count

    print()
    print("=" * 65)
    print("DONE")
    print("=" * 65)
    print(f"Total:    {len(results)}")
    print(f"Success:  {success_count}")
    print(f"Failed:   {failed_count}")
    print()
    print(f"Images:   {OUTPUT_DIR}")
    print(f"Report:   {report_path}")


if __name__ == "__main__":
    main()