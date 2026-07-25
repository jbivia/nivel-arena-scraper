"""Tests for the JPG -> transparent PNG conversion pipeline."""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="opencv-python-headless is not installed")

import convert_to_png  # noqa: E402
from convert_to_png import LEAK_RATIO, collect_inputs, convert_with_safety, process_image  # noqa: E402


def write_card(path, size=400, border=2):
    """A white-bordered card with an opaque black centre.

    The border is deliberately thin: a thick one would make the background a
    double-digit percentage of the image and legitimately trip the leak guard.
    """
    img = np.full((size, size, 3), 255, np.uint8)
    img[border:-border, border:-border] = 0
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


class TestConvertWithSafety:
    def test_normal_card_is_converted(self, dirs):
        downloads, processed = dirs
        src = write_card(downloads / "card.jpg")

        assert convert_with_safety(src, processed) is True
        assert (processed / "card.png").exists()

    def test_persistent_leak_is_skipped_and_output_removed(self, dirs):
        downloads, processed = dirs
        # An all-white image floods to 100% transparency at any tolerance.
        src = write_all_white(downloads / "blank.jpg")

        assert convert_with_safety(src, processed) is False
        assert not (processed / "blank.png").exists(), "leaked PNG must not be left behind"

    def test_leak_threshold_is_the_documented_value(self):
        assert LEAK_RATIO == pytest.approx(0.045)


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
        monkeypatch.setattr(convert_to_png, "MAX_INPUT_BYTES", 10)
        (downloads / "huge.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        assert collect_inputs(downloads, processed) == []


def test_decoder_pixel_limit_is_configured():
    """The bomb guard must be applied before cv2 loads its decoders."""
    import os

    assert int(os.environ["OPENCV_IO_MAX_IMAGE_PIXELS"]) == 64 * 1024 * 1024
