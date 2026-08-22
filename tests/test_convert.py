"""Tests for the JPG -> transparent PNG conversion pipeline."""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="opencv-python-headless is not installed")

from nivel.infrastructure.nikke.image import png_converter  # noqa: E402
from nivel.infrastructure.nikke.image.png_converter import (  # noqa: E402
    collect_inputs,
    convert_card,
    corner_radius,
    find_card_rects,
    process_image,
)


def write_card(path, size=400, border=2):
    """A white-bordered card with an opaque black centre."""
    img = np.full((size, size, 3), 255, np.uint8)
    img[border:-border, border:-border] = 0
    cv2.imwrite(str(path), img)
    return path


def write_rounded_card(path, width=370, height=515, radius=14):
    """A single dark card filling the frame, with rounded white corners."""
    img = np.full((height, width, 3), 255, np.uint8)
    mask = np.zeros((height, width), np.uint8)
    cv2.rectangle(mask, (radius, 0), (width - 1 - radius, height - 1), 255, cv2.FILLED)
    cv2.rectangle(mask, (0, radius), (width - 1, height - 1 - radius), 255, cv2.FILLED)
    for cx, cy in (
        (radius, radius),
        (width - 1 - radius, radius),
        (radius, height - 1 - radius),
        (width - 1 - radius, height - 1 - radius),
    ):
        cv2.circle(mask, (cx, cy), radius, 255, cv2.FILLED)
    img[mask == 255] = 40
    cv2.imwrite(str(path), img)
    return path


def write_stacked_pair(path, width=370, height=515, margin=5, gutter=8):
    """Two landscape cards stacked with a white gutter between them.

    This is the SB02-001 layout: the white to remove is not only at the outer
    corners but in a band across the middle of the frame.
    """
    img = np.full((height, width, 3), 255, np.uint8)
    split = height // 2
    img[0 : split - gutter // 2, margin : width - margin] = 40
    img[split + gutter // 2 :, margin : width - margin] = 40
    cv2.imwrite(str(path), img)
    return path


def write_all_white(path, size=64):
    cv2.imwrite(str(path), np.full((size, size, 3), 255, np.uint8))
    return path


def write_no_white_border(path, size=64):
    cv2.imwrite(str(path), np.zeros((size, size, 3), np.uint8))
    return path


@pytest.fixture
def dirs(tmp_path):
    downloads = tmp_path / "downloads"
    processed = tmp_path / "processed"
    downloads.mkdir()
    processed.mkdir()
    return downloads, processed


class TestFindCardRects:
    def test_single_card_spans_the_frame(self):
        background = np.zeros((515, 370), bool)
        assert find_card_rects(background) == [(0, 0, 369, 514)]

    def test_gutter_splits_the_frame_in_two(self):
        background = np.zeros((515, 370), bool)
        background[:, :5] = True
        background[:, 365:] = True
        background[256:259, :] = True

        assert find_card_rects(background) == [(5, 0, 364, 255), (5, 259, 364, 514)]

    def test_all_background_finds_nothing(self):
        assert find_card_rects(np.ones((64, 64), bool)) == []

    def test_a_thin_bright_band_is_not_a_gutter(self):
        """A one-pixel white line inside the artwork must not split the card."""
        background = np.zeros((515, 370), bool)
        background[300, :] = True

        assert find_card_rects(background) == [(0, 0, 369, 514)]


class TestCornerRadius:
    def test_square_corners_measure_zero(self):
        assert corner_radius(np.zeros((515, 370), bool), (0, 0, 369, 514)) == 0

    def test_white_artwork_at_one_corner_does_not_inflate_the_radius(self):
        """The minimum across four corners is the estimate artwork cannot skew."""
        background = np.zeros((515, 370), bool)
        # A genuine 6px arc at every corner...
        for x, y in ((0, 0), (364, 0), (0, 509), (364, 509)):
            background[y : y + 6, x : x + 6] = True
        # ...plus a slab of white artwork bleeding out of the top-left one.
        background[0:40, 0:40] = True

        assert corner_radius(background, (0, 0, 369, 514)) == 6


class TestProcessImage:
    def test_makes_white_border_transparent(self, dirs):
        downloads, processed = dirs
        src = write_card(downloads / "card.jpg")

        success, ratio = process_image(src, processed)

        assert success
        out = cv2.imread(str(processed / "card.png"), cv2.IMREAD_UNCHANGED)
        assert out.shape[2] == 4, "output must have an alpha channel"
        assert out[0, 0, 3] == 0, "corner should be transparent"
        assert out[200, 200, 3] == 255, "artwork centre must stay opaque"
        assert 0 < ratio < 1

    def test_rounded_corners_are_cut_and_the_card_is_untouched(self, dirs):
        downloads, processed = dirs
        src = write_rounded_card(downloads / "card.jpg")

        assert process_image(src, processed)[0]

        out = cv2.imread(str(processed / "card.png"), cv2.IMREAD_UNCHANGED)
        alpha = out[:, :, 3]
        assert alpha[0, 0] == 0, "outside the arc must be transparent"
        assert alpha[0, 185] == 255, "the straight top edge must stay opaque"
        assert alpha[257, 185] == 255, "the centre must stay opaque"
        # A single card only loses its four corners: about 0.1% of the frame.
        assert np.count_nonzero(alpha == 0) / alpha.size < 0.01

    def test_gutter_between_stacked_cards_is_transparent(self, dirs):
        downloads, processed = dirs
        src = write_stacked_pair(downloads / "SB02-001.jpg")

        assert process_image(src, processed)[0]

        out = cv2.imread(str(processed / "SB02-001.png"), cv2.IMREAD_UNCHANGED)
        alpha = out[:, :, 3]
        assert alpha[257, 185] == 0, "the gutter between the two cards must be transparent"
        assert alpha[185, 2] == 0, "the side margin must be transparent"
        assert alpha[128, 185] == 255, "the upper card must stay opaque"
        assert alpha[385, 185] == 255, "the lower card must stay opaque"

    def test_bright_artwork_touching_a_corner_is_not_eaten(self, dirs):
        """The failure mode of a colour flood fill: white art next to a corner.

        The fill escaped through the corner and hollowed the card out; a
        geometric mask cannot reach past the rectangle it was built from.
        """
        downloads, processed = dirs
        src = write_rounded_card(downloads / "bright.jpg")
        img = cv2.imread(str(src), cv2.IMREAD_COLOR)
        # Near-white artwork reaching into the top-left corner, the way a light
        # illustration does. It stops short of the frame edge, so no full row
        # or column reads as background.
        img[6:200, 6:200] = 252
        cv2.imwrite(str(src), img)

        success, ratio = process_image(src, processed)

        assert success
        assert ratio < 0.01, "at most the corners may be removed"
        out = cv2.imread(str(processed / "bright.png"), cv2.IMREAD_UNCHANGED)
        assert out[20, 20, 3] == 255, "bright artwork inside the card must stay opaque"

    def test_unreadable_file_fails_gracefully(self, dirs):
        downloads, processed = dirs
        bad = downloads / "broken.jpg"
        bad.write_bytes(b"not an image")

        assert process_image(bad, processed) == (False, 0.0)

    def test_no_white_corner_leaves_image_opaque(self, dirs):
        downloads, processed = dirs
        src = write_no_white_border(downloads / "dark.jpg")

        success, ratio = process_image(src, processed)

        assert success
        assert ratio == 0.0

    def test_all_white_image_is_refused(self, dirs):
        downloads, processed = dirs
        src = write_all_white(downloads / "blank.jpg")

        assert process_image(src, processed) == (False, 0.0)

    def test_low_coverage_is_refused(self, dirs, monkeypatch):
        """A card found in only a corner of the frame means the scan misread it."""
        downloads, processed = dirs
        img = np.full((400, 400, 3), 255, np.uint8)
        img[0:100, 0:100] = 0
        src = downloads / "sliver.jpg"
        cv2.imwrite(str(src), img)

        assert process_image(src, processed) == (False, 0.0)

    def test_too_many_rectangles_is_refused(self, dirs, monkeypatch):
        monkeypatch.setattr(png_converter, "MAX_CARDS", 1)
        downloads, processed = dirs
        src = write_stacked_pair(downloads / "pair.jpg")

        assert process_image(src, processed) == (False, 0.0)


class TestConvertCard:
    def test_normal_card_is_converted(self, dirs):
        downloads, processed = dirs
        src = write_card(downloads / "card.jpg")

        assert convert_card(src, processed) is True
        assert (processed / "card.png").exists()

    def test_unreadable_layout_writes_no_output(self, dirs):
        downloads, processed = dirs
        src = write_all_white(downloads / "blank.jpg")

        assert convert_card(src, processed) is False
        assert not (processed / "blank.png").exists()


class TestCollectInputs:
    def test_picks_up_mixed_case_suffixes(self, dirs):
        downloads, processed = dirs
        for name in ("a.jpg", "b.JPG", "c.jpeg", "d.JPEG"):
            (downloads / name).write_bytes(b"\xff\xd8\xff")

        found = {p.name for p in collect_inputs(downloads, processed)}
        assert found == {"a.jpg", "b.JPG", "c.jpeg", "d.JPEG"}

    def test_ignores_non_images(self, dirs):
        downloads, processed = dirs
        (downloads / "a.jpg").write_bytes(b"\xff\xd8\xff")
        (downloads / "notes.txt").write_bytes(b"hello")
        (downloads / "sub").mkdir()

        assert [p.name for p in collect_inputs(downloads, processed)] == ["a.jpg"]

    def test_drops_stem_collisions(self, dirs):
        downloads, processed = dirs
        # Both would write to card.png and race inside the process pool.
        (downloads / "card.jpg").write_bytes(b"\xff\xd8\xff")
        (downloads / "card.JPEG").write_bytes(b"\xff\xd8\xff")

        assert len(collect_inputs(downloads, processed)) == 1

    def test_skips_already_converted(self, dirs):
        downloads, processed = dirs
        (downloads / "card.jpg").write_bytes(b"\xff\xd8\xff")
        (processed / "card.png").write_bytes(b"\x89PNG")

        assert collect_inputs(downloads, processed) == []

    def test_force_reconverts(self, dirs):
        downloads, processed = dirs
        (downloads / "card.jpg").write_bytes(b"\xff\xd8\xff")
        (processed / "card.png").write_bytes(b"\x89PNG")

        assert len(collect_inputs(downloads, processed, force=True)) == 1

    def test_skips_oversized_input(self, dirs, monkeypatch):
        downloads, processed = dirs
        monkeypatch.setattr(png_converter, "MAX_INPUT_BYTES", 10)
        (downloads / "huge.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        assert collect_inputs(downloads, processed) == []


def test_decoder_pixel_limit_is_configured():
    """The bomb guard must be applied before cv2 loads its decoders."""
    import os

    assert int(os.environ["OPENCV_IO_MAX_IMAGE_PIXELS"]) == 64 * 1024 * 1024
