"""Fetch catalogue fields for cards that were downloaded before they existed."""

import logging

from nivel.application.nikke.failure_policy import MAX_CONSECUTIVE_PAGE_FAILURES
from nivel.domain.nikke.entity.card import Card
from nivel.infrastructure.nikke.parsing import card_metadata

log = logging.getLogger("scraper")


class BackfillMetadata:
    """Re-hits the detail endpoint for rows that have no catalogue entry."""

    def __init__(self, board, cards, history):
        self.board = board
        self.cards = cards
        self.history = history

    def execute(self, dry_run=True, limit=None, force=False):
        """Fetch catalogue fields for cards already downloaded.

        Images are never re-downloaded: this only re-hits the detail endpoint,
        which is what the scraper does anyway for every card it walks past. The
        connection is autocommit, so an interrupted run keeps the rows it has
        already written and the next run picks up where it stopped.

        Returns ``(processed, failures)``.
        """
        # --force refreshes everything downloaded; the default only fills the
        # rows that have no catalogue entry at all.
        rows = self.history.downloaded_entries(limit) if force else self.cards.wr_ids_without_metadata(limit)

        if dry_run:
            # Deliberately makes no requests: a preview that hammered the site
            # for 500 cards would be worse than the operation it previews.
            log.info(
                "Dry run: metadata would be fetched for %d card(s)%s.",
                len(rows),
                " (refreshing rows that already have it)" if force else "",
            )
            return len(rows), 0

        if not rows:
            log.info("Nothing to backfill: every scraped card already has metadata.")
            return 0, 0

        # One robots check for the endpoint, rather than the same question 500
        # times over.
        if not self.board.may_fetch(self.board.ajax_url):
            log.error("robots.txt disallows the detail endpoint; nothing to do.")
            return 0, 0

        processed, failures, consecutive_failures = 0, 0, 0
        for index, (wr_id, image_filename) in enumerate(rows):
            if index:
                self.board.sleep_between_cards()

            try:
                details = card_metadata.parse_card_details(self.board.get_card_details(wr_id))
            except Exception as exc:
                failures += 1
                consecutive_failures += 1
                log.error(
                    "Failed to fetch metadata for wr_id %s (%d/%d consecutive failures): %s",
                    wr_id,
                    consecutive_failures,
                    MAX_CONSECUTIVE_PAGE_FAILURES,
                    exc,
                )
                if consecutive_failures >= MAX_CONSECUTIVE_PAGE_FAILURES:
                    log.error("Giving up after %d consecutive failures.", consecutive_failures)
                    break
                continue

            consecutive_failures = 0
            if self.cards.upsert(Card.from_details(wr_id, details, image_filename)):
                processed += 1
                log.info("Stored metadata for wr_id %s (%s).", wr_id, details["card_number"])
            else:
                failures += 1

        log.info("Backfill complete: %d stored, %d failed (of %d rows).", processed, failures, len(rows))
        return processed, failures
