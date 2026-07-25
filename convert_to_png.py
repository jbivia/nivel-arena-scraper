"""Convert scraped card JPGs into PNGs with the white background removed.

Input images are downloaded from an untrusted plaintext-HTTP source and then
handed to OpenCV's native decoders, so the decode step is bounded before cv2 is
ever imported.
"""

import os

# Must be set before cv2 is imported: it is read once at extension load time and
# caps how large an image the decoders will allocate (decompression bombs).
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(64 * 1024 * 1024))

import argparse  # noqa: E402
import logging  # noqa: E402
from concurrent.futures import ProcessPoolExecutor, as_completed  # noqa: E402
from pathlib import Path  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("convert")

IMAGE_SUFFIXES = {".jpg", ".jpeg"}

# Refuse to decode anything larger than this. Real cards are ~200 KB.
MAX_INPUT_BYTES = 32 * 1024 * 1024

# A corner pixel must be at least this bright in every channel to seed a fill.
WHITE_THRESHOLD = 230

# Above this fraction of transparent pixels the fill has almost certainly
# leaked out of the background and into the artwork.
LEAK_RATIO = 0.045

DEFAULT_TOLERANCE = 10
FALLBACK_TOLERANCE = 3


def process_image(file_path, output_dir, tolerance=DEFAULT_TOLERANCE):
    """Flood-fill the white background to transparent.

    Returns ``(success, transparency_ratio)``.
    """
    # Each worker is its own process; letting OpenCV also fan out across every
    # core oversubscribes the machine by workers x cores.
    cv2.setNumThreads(1)

    img = cv2.imread(str(file_path), cv2.IMREAD_COLOR)
    if img is None:
        log.error("Could not read image: %s", file_path)
        return False, 0.0

    img_bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    h, w = img.shape[:2]
    total_pixels = h * w

    # floodFill requires a mask two pixels larger in each dimension.
    mask = np.zeros((h + 2, w + 2), np.uint8)
    corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    flags = 4 | cv2.FLOODFILL_MASK_ONLY | (1 << 8)

    for x, y in corners:
        # OpenCV is BGR-ordered; seed only from a corner that is already white.
        if not np.all(img[y, x] > WHITE_THRESHOLD):
            continue
        if mask[y + 1, x + 1] != 0:
            continue
        cv2.floodFill(
            img,
            mask,
            (x, y),
            0,
            loDiff=(tolerance,) * 3,
            upDiff=(tolerance,) * 3,
            flags=flags,
        )

    actual_mask = mask[1:-1, 1:-1] == 1
    transparency_ratio = float(np.count_nonzero(actual_mask)) / total_pixels
    img_bgra[actual_mask, 3] = 0

    png_path = output_dir / (file_path.stem + ".png")
    if cv2.imwrite(str(png_path), img_bgra):
        return True, transparency_ratio

    log.error("Failed to write output PNG: %s", png_path)
    return False, 0.0


def convert_with_safety(file_path, output_dir):
    """Convert at the default tolerance, retrying tighter if a leak is detected."""
    success, ratio = process_image(file_path, output_dir, tolerance=DEFAULT_TOLERANCE)
    if not success:
        return False

    if ratio > LEAK_RATIO:
        log.warning(
            "Possible leak in %s (%.1f%% transparent). Retrying with tolerance %d...",
            file_path.name,
            ratio * 100,
            FALLBACK_TOLERANCE,
        )
        success, ratio = process_image(file_path, output_dir, tolerance=FALLBACK_TOLERANCE)
        if not success:
            return False

        if ratio > LEAK_RATIO:
            log.error(
                "Persistent leak in %s (%.1f%% transparent). Skipping to protect artwork.",
                file_path.name,
                ratio * 100,
            )
            # Remove the leaked PNG so a partial result is never mistaken for good output.
            (output_dir / (file_path.stem + ".png")).unlink(missing_ok=True)
            return False

    log.info("Processed: %s (transparency %.1f%%)", file_path.name, ratio * 100)
    return True


def collect_inputs(downloads_dir, output_dir, force=False):
    """List images to convert, dropping oversized files and stem collisions."""
    candidates = sorted(
        p for p in downloads_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )

    selected = {}
    for path in candidates:
        if path.stat().st_size > MAX_INPUT_BYTES:
            log.warning(
                "Skipping %s: %d bytes exceeds the %d byte limit.",
                path.name,
                path.stat().st_size,
                MAX_INPUT_BYTES,
            )
            continue

        # Two inputs sharing a stem (card.jpg / card.JPEG) map to one output;
        # converting both would have workers racing on the same PNG.
        if path.stem in selected:
            log.warning("Skipping %s: output name collides with %s.", path.name, selected[path.stem].name)
            continue

        if not force and (output_dir / (path.stem + ".png")).exists():
            log.debug("Skipping %s: already converted.", path.name)
            continue

        selected[path.stem] = path

    return list(selected.values())


def _worker(args):
    """Top-level worker function for ProcessPoolExecutor (must be picklable)."""
    file_path, output_dir = args
    return convert_with_safety(file_path, output_dir)


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
        futures = {executor.submit(_worker, (img, args.processed_dir)): img for img in image_files}
        for future in as_completed(futures):
            img = futures[future]
            try:
                if future.result():
                    success_count += 1
            except Exception as exc:
                log.error("Error processing %s: %s", img.name, exc)

    log.info("Finished. Successfully processed %d/%d images.", success_count, len(image_files))
    # Individual images are legitimately skipped when the fill leaks, so only a
    # run that converted nothing at all counts as a failure.
    return 0 if success_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
