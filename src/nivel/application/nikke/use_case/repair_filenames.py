"""Repoint history rows at the files they were actually saved as.

Needs the downloads directory but no network: this is a reconciliation between
what the database says and what is on disk, so it takes a path rather than the
board client.
"""

import logging
import re

log = logging.getLogger("scraper")


class RepairFilenames:
    """Matches history rows against the images actually present."""

    def __init__(self, downloads_dir, history):
        self.downloads_dir = downloads_dir
        self.history = history

    def execute(self, dry_run=True):
        """Repoint DB rows written before the saved-filename fix.

        Early versions recorded the *source* filename instead of the name the
        image was saved under, so those rows point at files that do not exist.
        A row is only repaired when exactly one unclaimed file on disk matches
        its card_id, so ambiguous variants are left alone.
        """
        rows = self.history.all_entries()

        on_disk = {p.name for p in self.downloads_dir.glob("*.jpg")}
        claimed = {name for _, _, name in rows if name in on_disk}

        repaired, ambiguous, unresolved = 0, 0, 0
        updates = []
        for wr_id, card_id, image_filename in rows:
            if image_filename in on_disk:
                continue

            pattern = re.compile(rf"^{re.escape(card_id)}(-\d{{2}})?\.jpg$")
            candidates = sorted(n for n in on_disk - claimed if pattern.match(n))

            if len(candidates) == 1:
                new_name = candidates[0]
                log.info("Repair wr_id %s: %r -> %r", wr_id, image_filename, new_name)
                updates.append((new_name, wr_id))
                claimed.add(new_name)
                repaired += 1
            elif len(candidates) > 1:
                log.warning("wr_id %s (%s): %d candidates, leaving alone.", wr_id, card_id, len(candidates))
                ambiguous += 1
            else:
                log.warning("wr_id %s (%s): no matching file on disk.", wr_id, card_id)
                unresolved += 1

        if not dry_run:
            self.history.repoint_filenames(updates)

        log.info(
            "%s: %d repaired, %d ambiguous, %d unresolved (of %d rows).",
            "Dry run" if dry_run else "Repair complete",
            repaired,
            ambiguous,
            unresolved,
            len(rows),
        )
        return repaired, ambiguous, unresolved
