"""Tests for filename sanitisation and board-link parsing."""

import pytest

from main import parse_card_link, safe_stem


class TestSafeStem:
    def test_keeps_normal_card_id(self):
        assert safe_stem("BT06-037", "fallback") == "BT06-037"

    def test_keeps_underscores_and_digits(self):
        assert safe_stem("ST08_016", "fallback") == "ST08_016"

    def test_keeps_korean_text(self):
        assert safe_stem("스킬", "fallback") == "스킬"

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "..",
            "/etc/shadow",
            "..\\..\\windows\\system32",
            "....//....//",
        ],
    )
    def test_strips_path_traversal(self, hostile):
        result = safe_stem(hostile, "fallback")
        assert "/" not in result
        assert "\\" not in result
        assert "." not in result
        assert not result.startswith("-")

    def test_empty_input_uses_fallback(self):
        assert safe_stem("", "unknown_42") == "unknown_42"

    def test_all_stripped_uses_fallback(self):
        assert safe_stem("///...", "unknown_42") == "unknown_42"

    def test_none_uses_fallback(self):
        assert safe_stem(None, "unknown_42") == "unknown_42"

    def test_leading_dash_is_stripped(self):
        # A leading dash would make the file look like a CLI flag to shell tools.
        assert not safe_stem("--rf", "fallback").startswith("-")

    def test_strips_control_characters(self):
        assert safe_stem("BT06\x00\n\t-037", "fallback") == "BT06-037"

    def test_truncates_long_names_to_valid_utf8(self):
        result = safe_stem("가" * 500, "fallback")
        assert len(result.encode("utf-8")) <= 100
        result.encode("utf-8").decode("utf-8")  # must not be a broken sequence

    def test_truncation_does_not_split_multibyte_char(self):
        # 100 bytes / 3 bytes per char = 33 whole characters.
        assert safe_stem("한" * 40, "fallback") == "한" * 33


class TestParseCardLink:
    def test_parses_valid_link(self):
        href = "cfbf01_Urz5KNn6_72085eb.jpg♬1293"
        assert parse_card_link(href) == ("cfbf01_Urz5KNn6_72085eb.jpg", "1293")

    @pytest.mark.parametrize(
        "href",
        [
            None,
            "",
            "no-delimiter.jpg",
            "a♬b♬c",  # too many parts
            "♬1293",  # empty filename
            "file.jpg♬",  # empty wr_id
            "file.jpg♬not-a-number",  # non-numeric wr_id
            "file.jpg♬12'; DROP TABLE scraped_cards--",
        ],
    )
    def test_rejects_malformed(self, href):
        assert parse_card_link(href) is None

    @pytest.mark.parametrize(
        "href",
        [
            "../../../etc/passwd♬1",
            "..\\..\\secret♬1",
            "/absolute/path.jpg♬1",
            ".hidden.jpg♬1",
        ],
    )
    def test_rejects_path_traversal_in_filename(self, href):
        assert parse_card_link(href) is None

    def test_strips_surrounding_whitespace(self):
        assert parse_card_link("  file.jpg  ♬  1293  ") == ("file.jpg", "1293")
