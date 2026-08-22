"""Walk the board and download every card that is not already on disk."""

import logging

from nivel.application.nikke.failure_policy import MAX_CONSECUTIVE_PAGE_FAILURES
from nivel.domain.nikke.entity.card import Card
from nivel.domain.nikke.value_object.card_naming import parse_card_link, safe_stem
from nivel.infrastructure.nikke.parsing import card_metadata

log = logging.getLogger("scraper")


class ScrapeBoard:
    """Pages through the board, one card at a time, politely."""

    def __init__(self, board, cards, history):
        self.board = board
        self.cards = cards
        self.history = history

    def execute(self, max_pages=None):
        # Every card goes through the detail endpoint, so a board walk that is
        # not allowed to call it has nothing to do. Asked once here rather than
        # once per card, as the backfill does.
        if not self.board.may_fetch(self.board.ajax_url):
            log.error("robots.txt disallows the detail endpoint %s; nothing to scrape.", self.board.ajax_url)
            return

        page = 1
        consecutive_failures = 0

        while True:
            if max_pages is not None and page > max_pages:
                break

            list_url = self.board.list_page_url(page)
            if not self.board.may_fetch(list_url):
                break

            try:
                soup = self.board.get_html(list_url)
            except Exception as exc:
                consecutive_failures += 1
                log.error(
                    "Failed to retrieve page %d (%d/%d consecutive failures): %s",
                    page,
                    consecutive_failures,
                    MAX_CONSECUTIVE_PAGE_FAILURES,
                    exc,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    log.error("Giving up after %d consecutive page failures.", consecutive_failures)
                    break
                page += 1
                continue

            consecutive_failures = 0
            card_links = soup.select("div.gall_img a")
            if not card_links:
                log.info("No more items found on page %d. Board complete.", page)
                break

            for link in card_links:
                parsed = parse_card_link(link.get("href"))
                if parsed is None:
                    log.debug("Skipping unrecognised link: %r", link.get("href"))
                    continue

                img_filename, wr_id = parsed
                if self.history.is_already_scraped(wr_id):
                    log.info("Skipping wr_id %s - already scraped.", wr_id)
                    continue

                self.scrape_card(wr_id, img_filename)
                self.board.sleep_between_cards()

            page += 1

    def scrape_card(self, wr_id, img_filename):
        try:
            detail_soup = self.board.get_card_details(wr_id)
        except Exception as exc:
            log.error("Failed to retrieve details for wr_id %s: %s", wr_id, exc)
            return

        # A card whose metadata will not parse is still worth downloading, so a
        # parse failure degrades to "image only" instead of losing the card.
        details = None
        try:
            details = card_metadata.parse_card_details(detail_soup)
        except Exception as exc:
            log.error("Could not parse metadata for wr_id %s: %s", wr_id, exc)

        card_id = f"unknown_{wr_id}"
        if details and details["card_number"]:
            card_id = safe_stem(details["card_number"], fallback=f"unknown_{wr_id}")

        full_src = self.board.image_url(img_filename)

        try:
            actual_filename = self.board.download_image(full_src, f"{card_id}.jpg")
        except Exception as exc:
            log.error("Failed to download %s: %s", full_src, exc)
            return

        if actual_filename:
            self.history.mark_as_scraped(wr_id, card_id, actual_filename)
            if details:
                self.cards.upsert(Card.from_details(wr_id, details, actual_filename))
