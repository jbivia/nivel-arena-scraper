"""Tests for the scraper's database, download and board-walking behaviour."""

import io

import pytest
import requests

import main
from main import MAX_IMAGE_BYTES, NivelArenaScraper

JPEG_BODY = b"\xff\xd8\xff\xe0" + b"\x00" * 512


@pytest.fixture
def scraper(tmp_path, monkeypatch):
    # No sleeping and no robots fetch in tests.
    monkeypatch.setattr(main.time, "sleep", lambda _: None)
    with NivelArenaScraper(
        "http://example.test",
        "cardlists",
        db_path=tmp_path / "data" / "test.db",
        downloads_dir=tmp_path / "downloads",
        obey_robots=False,
    ) as instance:
        yield instance


class FakeResponse:
    """Minimal stand-in for a streamed requests.Response."""

    def __init__(self, body=JPEG_BODY, status=200, headers=None, url="http://example.test/img.jpg"):
        self._body = body
        self.status_code = status
        self.url = url
        self.headers = (
            headers
            if headers is not None
            else {
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(body)),
            }
        )

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        stream = io.BytesIO(self._body)
        while chunk := stream.read(chunk_size):
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub_get(scraper, response):
    scraper.session.get = lambda *a, **kw: response


class TestDatabase:
    def test_round_trip(self, scraper):
        assert not scraper.is_already_scraped("1")
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001.jpg")
        assert scraper.is_already_scraped("1")

    def test_duplicate_insert_does_not_raise(self, scraper):
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001.jpg")
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001-01.jpg")  # must not raise
        rows = scraper._conn.execute("SELECT image_filename FROM scraped_cards").fetchall()
        assert rows == [("BT06-001.jpg",)]

    def test_wal_mode_enabled(self, scraper):
        mode = scraper._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


class TestDownloadImage:
    def test_saves_file_and_returns_name(self, scraper):
        stub_get(scraper, FakeResponse())
        assert scraper.download_image("http://example.test/a.jpg", "BT06-001.jpg") == "BT06-001.jpg"
        assert (scraper.downloads_dir / "BT06-001.jpg").read_bytes() == JPEG_BODY

    def test_deduplicates_existing_filename(self, scraper):
        (scraper.downloads_dir / "BT06-001.jpg").write_bytes(b"old")
        stub_get(scraper, FakeResponse())
        assert scraper.download_image("http://example.test/a.jpg", "BT06-001.jpg") == "BT06-001-01.jpg"
        assert (scraper.downloads_dir / "BT06-001.jpg").read_bytes() == b"old"

    def test_rejects_cross_host_redirect(self, scraper):
        stub_get(scraper, FakeResponse(url="http://evil.test/a.jpg"))
        assert scraper.download_image("http://example.test/a.jpg", "x.jpg") is None
        assert list(scraper.downloads_dir.iterdir()) == []

    def test_rejects_non_image_content_type(self, scraper):
        stub_get(scraper, FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}))
        assert scraper.download_image("http://example.test/a.jpg", "x.jpg") is None
        assert list(scraper.downloads_dir.iterdir()) == []

    def test_rejects_html_body_masquerading_as_image(self, scraper):
        stub_get(scraper, FakeResponse(body=b"<html>404 not found</html>"))
        assert scraper.download_image("http://example.test/a.jpg", "x.jpg") is None
        assert list(scraper.downloads_dir.iterdir()) == []

    def test_rejects_oversized_declared_length(self, scraper):
        stub_get(
            scraper,
            FakeResponse(headers={"Content-Type": "image/jpeg", "Content-Length": str(MAX_IMAGE_BYTES + 1)}),
        )
        assert scraper.download_image("http://example.test/a.jpg", "x.jpg") is None

    def test_rejects_body_exceeding_cap_without_content_length(self, scraper, monkeypatch):
        monkeypatch.setattr(main, "MAX_IMAGE_BYTES", 1024)
        big = b"\xff\xd8\xff" + b"\x00" * 4096
        stub_get(scraper, FakeResponse(body=big, headers={"Content-Type": "image/jpeg"}))
        assert scraper.download_image("http://example.test/a.jpg", "x.jpg") is None
        # Nothing partial is left behind.
        assert list(scraper.downloads_dir.iterdir()) == []

    def test_rejects_empty_body(self, scraper):
        stub_get(scraper, FakeResponse(body=b"", headers={"Content-Type": "image/jpeg"}))
        assert scraper.download_image("http://example.test/a.jpg", "x.jpg") is None

    def test_leaves_no_part_file_on_failure(self, scraper):
        stub_get(scraper, FakeResponse(body=b"<html>nope</html>"))
        scraper.download_image("http://example.test/a.jpg", "x.jpg")
        assert not any(p.name.endswith(".part") for p in scraper.downloads_dir.iterdir())


class TestRepairFilenames:
    def test_repairs_unique_match(self, scraper):
        (scraper.downloads_dir / "BT06-037.jpg").write_bytes(JPEG_BODY)
        scraper.mark_as_scraped("1065", "BT06-037", "source_hash_abc.jpg")

        scraper.repair_filenames(dry_run=False)

        stored = scraper._conn.execute(
            "SELECT image_filename FROM scraped_cards WHERE wr_id = '1065'"
        ).fetchone()[0]
        assert stored == "BT06-037.jpg"

    def test_dry_run_changes_nothing(self, scraper):
        (scraper.downloads_dir / "BT06-037.jpg").write_bytes(JPEG_BODY)
        scraper.mark_as_scraped("1065", "BT06-037", "source_hash_abc.jpg")

        repaired, _, _ = scraper.repair_filenames(dry_run=True)

        assert repaired == 1
        stored = scraper._conn.execute(
            "SELECT image_filename FROM scraped_cards WHERE wr_id = '1065'"
        ).fetchone()[0]
        assert stored == "source_hash_abc.jpg"

    def test_leaves_ambiguous_variants_alone(self, scraper):
        (scraper.downloads_dir / "BT06-037.jpg").write_bytes(JPEG_BODY)
        (scraper.downloads_dir / "BT06-037-01.jpg").write_bytes(JPEG_BODY)
        scraper.mark_as_scraped("1065", "BT06-037", "source_hash_abc.jpg")

        repaired, ambiguous, _ = scraper.repair_filenames(dry_run=False)

        assert (repaired, ambiguous) == (0, 1)

    def test_does_not_double_claim_a_file(self, scraper):
        (scraper.downloads_dir / "BT06-037.jpg").write_bytes(JPEG_BODY)
        scraper.mark_as_scraped("1065", "BT06-037", "BT06-037.jpg")  # already correct
        scraper.mark_as_scraped("1066", "BT06-037", "source_hash_abc.jpg")

        repaired, _, unresolved = scraper.repair_filenames(dry_run=False)

        assert (repaired, unresolved) == (0, 1)


class TestScrapeBoard:
    def test_stops_on_empty_page(self, scraper, monkeypatch):
        pages = []

        def fake_get_html(url):
            pages.append(url)
            return main.BeautifulSoup("<html><body></body></html>", "html.parser")

        monkeypatch.setattr(scraper, "get_html", fake_get_html)
        scraper.scrape_board()
        assert len(pages) == 1

    def test_gives_up_after_consecutive_failures(self, scraper, monkeypatch):
        attempts = []

        def always_fails(url):
            attempts.append(url)
            raise requests.ConnectionError("boom")

        monkeypatch.setattr(scraper, "get_html", always_fails)
        scraper.scrape_board()
        assert len(attempts) == main.MAX_CONSECUTIVE_PAGE_FAILURES

    def test_survives_a_single_transient_failure(self, scraper, monkeypatch):
        calls = {"n": 0}

        def flaky(url):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("transient")
            return main.BeautifulSoup("<html></html>", "html.parser")

        monkeypatch.setattr(scraper, "get_html", flaky)
        scraper.scrape_board()
        assert calls["n"] == 2  # recovered and then hit the empty-page stop

    def test_respects_max_pages(self, scraper, monkeypatch):
        html = '<div class="gall_img"><a href="f.jpg♬1"></a></div>'
        monkeypatch.setattr(scraper, "get_html", lambda url: main.BeautifulSoup(html, "html.parser"))
        monkeypatch.setattr(scraper, "scrape_card", lambda *a: None)
        scraper.mark_as_scraped("1", "x", "x.jpg")  # so nothing is downloaded
        scraper.scrape_board(max_pages=2)  # must terminate

    def test_skips_already_scraped(self, scraper, monkeypatch):
        html = '<div class="gall_img"><a href="f.jpg♬1"></a></div>'
        seen = []
        monkeypatch.setattr(scraper, "get_html", lambda url: main.BeautifulSoup(html, "html.parser"))
        monkeypatch.setattr(scraper, "scrape_card", lambda wr_id, fn: seen.append(wr_id))
        scraper.mark_as_scraped("1", "x", "x.jpg")
        scraper.scrape_board(max_pages=1)
        assert seen == []
