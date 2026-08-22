"""Parsing tests for the card-detail response.

The fixtures are verbatim responses captured from the live endpoint, so the
parser is tested against the site's real markup -- malformed closing tags,
trailing spaces in labels and all -- rather than against a tidied-up idea of it.
"""

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from nivel.infrastructure.nikke.parsing import card_metadata

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return BeautifulSoup((FIXTURES / name).read_text(encoding="utf-8"), "html.parser")


@pytest.fixture
def unit():
    return card_metadata.parse_card_details(load("unit_full.html"))


@pytest.fixture
def skill():
    return card_metadata.parse_card_details(load("skill_nulls.html"))


@pytest.fixture
def item():
    return card_metadata.parse_card_details(load("item_equip.html"))


class TestFullyPopulatedUnit:
    def test_header_fields(self, unit):
        assert unit["card_number"] == "ST08-014"
        assert unit["set_code"] == "ST08"
        assert unit["name"] == "타락의 유열 샬럿"
        assert unit["card_type"] == "유닛"
        assert unit["element"] == "화염"

    def test_normalised_values(self, unit):
        assert unit["card_type_en"] == "unit"
        assert unit["element_en"] == "fire"

    def test_numeric_fields(self, unit):
        assert unit["cost"] == 6
        assert unit["power"] == 11000
        assert unit["hit"] == 2

    def test_rarity_is_kept_as_text(self, unit):
        assert unit["rarity"] == "SBR"

    def test_affiliation_splits_on_slash(self, unit):
        assert unit["affiliation"] == ["이펙트", "레기온"]

    def test_keywords_split_on_comma(self, unit):
        assert unit["keywords"] == ["패시브", "액티브"]

    def test_effect_keeps_icons_as_markers(self, unit):
        assert unit["effect"].startswith("[패시브] 자신의 패 1장마다 파워-1000.")
        assert "[액티브: 어택]" in unit["effect"]

    def test_effect_keeps_line_structure(self, unit):
        assert len(unit["effect"].splitlines()) == 2

    def test_trigger_is_separate_and_unlabelled(self, unit):
        assert unit["trigger_text"] == "이 카드를 자신의 패에 넣는다."
        assert "트리거" not in unit["effect"]

    def test_provenance(self, unit):
        assert unit["product_name"] == "이터널 리턴 니벨아레나 스페셜 부스터 팩 2026 SB02"
        assert unit["ip"] == "이터널 리턴"


class TestNullSentinels:
    """The site prints '-' where a field does not apply; that is a null."""

    def test_absent_numbers_are_none_not_zero(self, skill):
        assert skill["power"] is None
        assert skill["hit"] is None

    def test_absent_lists_are_empty(self, skill):
        assert skill["affiliation"] == []
        assert skill["keywords"] == []

    def test_present_fields_still_parse(self, skill):
        assert skill["cost"] == 4
        assert skill["card_type_en"] == "skill"
        assert skill["rarity"] == "P"

    def test_effect_without_icons(self, skill):
        assert skill["effect"].startswith("자신의 스킬 존에 있는")
        assert skill["trigger_text"] == "이 카드를 자신의 패에 넣는다."


class TestItemCard:
    def test_equip_condition_icon_is_preserved(self, item):
        assert item["effect"].startswith("[장착 조건: 이브]")
        assert "[가디언]" in item["effect"]

    def test_bold_markup_is_flattened_into_the_text(self, item):
        assert "상쇄〈스마트 마인〉" in item["effect"]

    def test_no_trigger_box(self, item):
        assert item["trigger_text"] is None

    def test_single_keyword(self, item):
        assert item["keywords"] == ["가디언"]
        assert item["card_type_en"] == "item"


class TestRarityVariants:
    """The same card number is reprinted at several rarities."""

    def test_variant_shares_the_number_but_not_the_rarity(self):
        variant = card_metadata.parse_card_details(load("variant_rarity.html"))
        assert variant["card_number"] == "BT05-071"
        assert variant["rarity"] == "SPR"
        assert variant["power"] == 4000

    def test_inline_element_icon_becomes_a_marker(self):
        variant = card_metadata.parse_card_details(load("variant_rarity.html"))
        assert variant["effect"].startswith("[믹스] [암드]")
        assert "[번개] 이외의 카드가" in variant["effect"]


class TestMalformedResponses:
    def test_empty_document_yields_all_nulls(self):
        details = card_metadata.parse_card_details(BeautifulSoup("", "html.parser"))
        assert details["card_number"] is None
        assert details["name"] is None
        assert details["cost"] is None
        assert details["affiliation"] == []
        assert details["effect"] is None

    def test_type_header_with_fewer_parts(self):
        soup = BeautifulSoup("<h2 id='type'>BT01-001</h2>", "html.parser")
        details = card_metadata.parse_card_details(soup)
        assert details["card_number"] == "BT01-001"
        assert details["card_type"] is None
        assert details["element"] is None

    def test_missing_content_div(self):
        soup = BeautifulSoup("<h2 id='subject'>이름</h2>", "html.parser")
        details = card_metadata.parse_card_details(soup)
        assert details["name"] == "이름"
        assert details["effect"] is None
        assert details["trigger_text"] is None

    def test_image_without_alt_is_dropped(self):
        soup = BeautifulSoup("<div id='content'><img src='x.png'> 효과.</div>", "html.parser")
        assert card_metadata.parse_card_details(soup)["effect"] == "효과."

    def test_non_numeric_cell_is_none(self):
        soup = BeautifulSoup("<table><tr><td class='h3'>코스트</td><td>없음</td></tr></table>", "html.parser")
        assert card_metadata.parse_card_details(soup)["cost"] is None

    def test_unmapped_values_keep_the_korean(self):
        soup = BeautifulSoup("<h2 id='type'>XX01-001 / 미지 / 미지원소</h2>", "html.parser")
        details = card_metadata.parse_card_details(soup)
        assert details["card_type"] == "미지"
        assert details["card_type_en"] is None
        assert details["element_en"] is None

    def test_parsing_does_not_mutate_the_caller_soup(self):
        soup = load("unit_full.html")
        card_metadata.parse_card_details(soup)
        # The trigger box is extracted from a copy, so a second parse of the
        # same soup must return the same thing.
        assert soup.select_one("p.triger_box") is not None
        assert card_metadata.parse_card_details(soup)["trigger_text"] is not None


class TestSetCode:
    @pytest.mark.parametrize(
        ("number", "expected"),
        [
            ("ST08-014", "ST08"),
            ("BT05-071", "BT05"),
            ("P-001", None),
            ("nonsense", None),
            ("", None),
            (None, None),
        ],
    )
    def test_set_code(self, number, expected):
        assert card_metadata.set_code(number) == expected
