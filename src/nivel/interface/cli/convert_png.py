"""Convert scraped JPGs into transparent PNGs.

A separate invocation on purpose: a scrape only downloads, and the conversion is
CPU-bound work over the whole downloads directory that has no reason to be
serialised behind the network politeness the scrape is bound by.
"""

import argparse
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from nivel.infrastructure.nikke.image.png_converter import collect_inputs, convert_one

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("convert")


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Convert card JPGs to transparent PNGs.")
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=Path(os.environ.get("SCRAPER_DOWNLOADS_DIR", "/app/downloads")),
        help="Directory of source JPGs (env: SCRAPER_DOWNLOADS_DIR).",
    )
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=Path(os.environ.get("SCRAPER_PROCESSED_DIR", "/app/processed")),
        help="Directory for output PNGs (env: SCRAPER_PROCESSED_DIR).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 1) // 2),
        help="Parallel worker processes. Default: half the available CPUs.",
    )
    parser.add_argument("--force", action="store_true", help="Reconvert images that already have a PNG.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.downloads_dir.is_dir():
        log.error("Downloads directory does not exist: %s", args.downloads_dir)
        return 1

    args.processed_dir.mkdir(parents=True, exist_ok=True)

    image_files = collect_inputs(args.downloads_dir, args.processed_dir, force=args.force)
    if not image_files:
        log.warning("No images to convert in %s.", args.downloads_dir)
        return 0

    workers = max(1, args.workers)
    log.info("Converting %d images with %d workers...", len(image_files), workers)

    success_count = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(convert_one, (img, args.processed_dir)): img for img in image_files}
        for future in as_completed(futures):
            img = futures[future]
            try:
                if future.result():
                    success_count += 1
            except Exception as exc:
                log.error("Error processing %s: %s", img.name, exc)

    log.info("Finished. Successfully processed %d/%d images.", success_count, len(image_files))
    # An individual image is legitimately refused when its layout will not
    # parse, so only a run that converted nothing at all counts as a failure.
    return 0 if success_count else 1
