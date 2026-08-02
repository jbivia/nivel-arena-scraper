"""Convert scraped card JPGs into PNGs with the white background removed.

The board serves one JPEG per entry and no PNG or WebP variant exists at the
source, so the alpha channel has to be reconstructed here. It is reconstructed
*geometrically* rather than by colour: every entry is one or more rounded
rectangles laid out on a white canvas, so the background is found by locating
those rectangles and masking everything outside them. A colour flood fill
cannot do this safely -- on a light card the white outside the artwork is
contiguous with white *inside* it, and the fill escapes and eats the card.

Three layouts occur in the wild, and the projection scan below handles all
three without special-casing:

  * one card filling the frame edge to edge (corner radius ~14px at 370x515);
  * one card inset by a few pixels of white margin;
  * two landscape cards stacked vertically, separated by a white gutter, each
    with its own four rounded corners (e.g. SB02-001).

Input images are downloaded from an untrusted plaintext-HTTP source and then
handed to OpenCV's native decoders, so the decode step is bounded before cv2 is
ever imported.
"""

import os

# Must be set before cv2 is imported: it is read once at extension load time and
# caps how large an image the decoders will allocate (decompression bombs).
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", str(64 * 1024 * 1024))

import argparse
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("convert")

IMAGE_SUFFIXES = {".jpg", ".jpeg"}

# Refuse to decode anything larger than this. Real cards are ~200 KB.
MAX_INPUT_BYTES = 32 * 1024 * 1024

# A pixel counts as background when its darkest channel is above this. Kept
# below pure white because the source is JPEG: the margins ring slightly.
WHITE_LEVEL = 228

# Fraction of a scanline that must be background for it to count as a margin or
# a gutter. Not 1.0: the arcs of the corners bordering a gutter leave a few
# non-background pixels in the rows either side of it.
BACKGROUND_LINE = 0.98

# A run of non-background shorter than this fraction of its axis is noise, not
# a card. Sized to admit the ~50% tall halves of a two-card composite while
# rejecting a stray bright band inside the artwork.
MIN_SPAN_RATIO = 0.10

# A gutter *inside* the frame is held to a higher standard than a margin at its
# edge, because a full-width bright row in the artwork looks exactly like one
# and splitting on it would cut a transparent line across the card. The
# observed gutter is 3px thick and, being flanked by the rounded corners of the
# cards either side of it, leaves a measurable amount of background in the
# lines just outside it. A bright row in the artwork has neither property.
MIN_GUTTER = 2
ARC_WINDOW = 3
MIN_ARC_EVIDENCE = 0.02

# No layout observed on this board has more than two cards; anything past this
# means the scan latched onto artwork and the result is not to be trusted.
MAX_CARDS = 4

# The detected rectangles must account for at least this much of the frame.
# This is the real correctness check: the old ratio-of-transparent-pixels guard
# could not tell a leak from a legitimately wide margin, and at 4.5% it sat
# roughly fifty times above the ~0.09% a correct single-card mask produces.
MIN_COVERAGE = 0.80

# A corner radius above this fraction of the rectangle's short side means the
# arc scan ran into white artwork instead of the card's edge.
MAX_RADIUS_RATIO = 0.12

# The mask is rasterised at this factor and box-filtered down, which is what
# antialiases the arcs. Drawing the parts with cv2.LINE_AA instead would leave
# seams where the rectangles and corner circles overlap.
SUPERSAMPLE = 4


def _runs(flags):
    """Yield ``(start, end)`` inclusive index ranges where ``flags`` is True."""
    out, start = [], None
    for i, flag in enumerate(flags):
        if flag:
            if start is None:
                start = i
        elif start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _flanked_by_arcs(fraction, start, end):
    """Whether the lines either side of a band carry rounded-corner residue."""
    before = fraction[max(0, start - ARC_WINDOW) : start]
    after = fraction[end + 1 : end + 1 + ARC_WINDOW]
    if not len(before) or not len(after):
        return False
    return before.mean() >= MIN_ARC_EVIDENCE and after.mean() >= MIN_ARC_EVIDENCE


def _card_spans(fraction, minimum_length):
    """Runs of the axis that are *not* margin or gutter."""
    length = len(fraction)

    background = np.zeros(length, bool)
    for start, end in _runs(fraction >= BACKGROUND_LINE):
        at_edge = start == 0 or end == length - 1
        if not at_edge and (end - start + 1 < MIN_GUTTER or not _flanked_by_arcs(fraction, start, end)):
            continue
        background[start : end + 1] = True

    spans = _runs(~background)
    return [s for s in spans if s[1] - s[0] + 1 >= minimum_length]


def find_card_rects(background):
    """Locate the card rectangles in a boolean background mask.

    Returns a list of ``(x0, y0, x1, y1)`` inclusive rectangles, empty when the
    layout could not be read. Margins and gutters are full-width or full-height
    bands of background, so projecting the mask onto each axis separates them
    from the cards without ever looking at what is inside a card.
    """
    height, width = background.shape

    rows = _card_spans(background.mean(axis=1), int(height * MIN_SPAN_RATIO))
    cols = _card_spans(background.mean(axis=0), int(width * MIN_SPAN_RATIO))
    if not rows or not cols:
        return []

    return [(x0, y0, x1, y1) for (x0, x1) in cols for (y0, y1) in rows]


def corner_radius(background, rect):
    """Measure the rounded-corner radius of one rectangle, in pixels.

    Each corner is scanned inwards for the height of its background arc and the
    smallest of the four is taken. White artwork touching a corner can only
    make that corner read *too large*, never too small, so the minimum is the
    one estimate the artwork cannot inflate.
    """
    x0, y0, x1, y1 = rect
    limit = int(min(x1 - x0, y1 - y0) * MAX_RADIUS_RATIO)
    if limit <= 0:
        return 0

    estimates = []
    for step_x, step_y in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        origin_x = x0 if step_x > 0 else x1
        origin_y = y0 if step_y > 0 else y1

        depth = 0
        for offset in range(limit + 1):
            y = origin_y + step_y * offset
            if not background[y, origin_x]:
                break
            depth = offset + 1
        estimates.append(depth)

    return min(*estimates, limit)


def build_alpha(shape, rects, radii):
    """Rasterise the union of rounded rectangles as an 8-bit alpha channel."""
    height, width = shape
    scale = SUPERSAMPLE
    canvas = np.zeros((height * scale, width * scale), np.uint8)

    for (x0, y0, x1, y1), radius in zip(rects, radii, strict=True):
        # Inclusive pixel bounds become half-open edges at the supersampled
        # scale, so the last pixel row and column are covered in full.
        left, top = x0 * scale, y0 * scale
        right, bottom = (x1 + 1) * scale - 1, (y1 + 1) * scale - 1
        r = radius * scale

        if r <= 0:
            cv2.rectangle(canvas, (left, top), (right, bottom), 255, cv2.FILLED)
            continue

        cv2.rectangle(canvas, (left + r, top), (right - r, bottom), 255, cv2.FILLED)
        cv2.rectangle(canvas, (left, top + r), (right, bottom - r), 255, cv2.FILLED)
        for cx, cy in (
            (left + r, top + r),
            (right - r, top + r),
            (left + r, bottom - r),
            (right - r, bottom - r),
        ):
            cv2.circle(canvas, (cx, cy), r, 255, cv2.FILLED)

    return cv2.resize(canvas, (width, height), interpolation=cv2.INTER_AREA)


def unmix_white(bgr, alpha):
    """Recover the card's own colour along the antialiased edge.

    A pixel on an arc is a blend of the card and the white it was flattened
    onto, so leaving it as-is draws a white fringe once the alpha is applied.
    Inverting the blend, ``card = (observed - (1 - a) * 255) / a``, is only
    meaningful where the pixel is partly covered.
    """
    partial = (alpha > 0) & (alpha < 255)
    if not partial.any():
        return bgr

    out = bgr.astype(np.float32)
    a = (alpha[partial].astype(np.float32) / 255.0)[:, None]
    out[partial] = (out[partial] - (1.0 - a) * 255.0) / a
    return np.clip(out, 0, 255).astype(np.uint8)


def process_image(file_path, output_dir):
    """Mask the white background of one card to transparent.

    Returns ``(success, transparency_ratio)``.
    """
    # Each worker is its own process; letting OpenCV also fan out across every
    # core oversubscribes the machine by workers x cores.
    cv2.setNumThreads(1)

    # A decode that trips OPENCV_IO_MAX_IMAGE_PIXELS raises out of the native
    # layer rather than returning None, so the bomb guard only actually guards
    # if that is caught here: uncaught, it would take the worker down with it.
    try:
        img = cv2.imread(str(file_path), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        log.error("Refusing %s: decoder rejected it (%s)", file_path.name, exc)
        return False, 0.0

    if img is None:
        log.error("Could not read image: %s", file_path)
        return False, 0.0

    height, width = img.shape[:2]
    total_pixels = height * width
    background = img.min(axis=2) > WHITE_LEVEL

    rects = find_card_rects(background)
    if not rects:
        log.error("No card found in %s: the frame reads as background throughout.", file_path.name)
        return False, 0.0

    if len(rects) > MAX_CARDS:
        log.error("Refusing %s: layout scan found %d rectangles.", file_path.name, len(rects))
        return False, 0.0

    covered = sum((x1 - x0 + 1) * (y1 - y0 + 1) for x0, y0, x1, y1 in rects)
    if covered < total_pixels * MIN_COVERAGE:
        log.error(
            "Refusing %s: detected cards cover only %.1f%% of the frame.",
            file_path.name,
            covered / total_pixels * 100,
        )
        return False, 0.0

    radii = [corner_radius(background, rect) for rect in rects]
    alpha = build_alpha((height, width), rects, radii)

    img_bgra = cv2.cvtColor(unmix_white(img, alpha), cv2.COLOR_BGR2BGRA)
    img_bgra[:, :, 3] = alpha

    transparency_ratio = float(np.count_nonzero(alpha == 0)) / total_pixels

    png_path = output_dir / (file_path.stem + ".png")
    if cv2.imwrite(str(png_path), img_bgra):
        log.debug("%s: %d card(s), radii %s", file_path.name, len(rects), radii)
        return True, transparency_ratio

    log.error("Failed to write output PNG: %s", png_path)
    return False, 0.0


def convert_card(file_path, output_dir):
    """Convert one card, logging the outcome."""
    success, ratio = process_image(file_path, output_dir)
    if not success:
        return False

    log.info("Processed: %s (transparency %.2f%%)", file_path.name, ratio * 100)
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
    return convert_card(file_path, output_dir)


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
    # An individual image is legitimately refused when its layout will not
    # parse, so only a run that converted nothing at all counts as a failure.
    return 0 if success_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
