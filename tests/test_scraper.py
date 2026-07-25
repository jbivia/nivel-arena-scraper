"""Tests for the scraper's database, download and board-walking behaviour."""

import io
import sqlite3

import pytest
import requests

import main
from main import MAX_IMAGE_BYTES, DatabaseNotConfigured, NivelArenaScraper

JPEG_BODY = b"\xff\xd8\xff\xe0" + b"\x00" * 512


@pytest.fixture
def scraper(tmp_path, monkeypatch, database_url):
    # No sleeping and no robots fetch in tests.
    monkeypatch.setattr(main.time, "sleep", lambda _: None)
    with NivelArenaScraper(
        "http://example.test",
        "cardlists",
        database_url=database_url,
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


class TestConnectionConfig:
    """These need no database: they cover the paths taken before one is opened."""

    def test_missing_url_is_a_clear_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv(main.DATABASE_URL_ENV, raising=False)
        with pytest.raises(DatabaseNotConfigured, match=main.DATABASE_URL_ENV):
            NivelArenaScraper("http://example.test", "cardlists", downloads_dir=tmp_path / "downloads")

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("postgres://nivel:hunter2@nivel-db:5432/nivel", "postgres://nivel:***@nivel-db:5432/nivel"),
            ("postgres://nivel@nivel-db:5432/nivel", "postgres://nivel@nivel-db:5432/nivel"),
            ("postgres://nivel-db/nivel", "postgres://nivel-db/nivel"),
        ],
    )
    def test_redacts_password_for_logging(self, url, expected):
        assert main.redact_conninfo(url) == expected

    def test_redaction_drops_query_and_fragment(self):
        # sslmode etc. are harmless, but a stray password= there would not be.
        redacted = main.redact_conninfo("postgres://u:p@h:5432/db?password=hunter2#frag")
        assert "hunter2" not in redacted


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

    def test_init_is_idempotent(self, scraper):
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001.jpg")
        scraper._init_db()  # CREATE TABLE IF NOT EXISTS must not drop anything
        assert scraper.is_already_scraped("1")

    def test_rows_are_timestamped(self, scraper):
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001.jpg")
        stamped = scraper._conn.execute(
            "SELECT scraped_at IS NOT NULL FROM scraped_cards WHERE wr_id = '1'"
        ).fetchone()[0]
        assert stamped


def details(**overrides):
    """A parsed-details dict with every key present, as the parser returns."""
    base = {
        "card_number": "BT06-001",
        "set_code": "BT06",
        "name": "누아르",
        "card_type": "유닛",
        "card_type_en": "unit",
        "element": "화염",
        "element_en": "fire",
        "cost": 2,
        "power": 2000,
        "hit": 1,
        "rarity": "R",
        "affiliation": ["이펙트", "테트라"],
        "keywords": ["엔트리"],
        "effect": "[엔트리] 이 턴이 끝날 때까지 조우 유닛의 파워 -3000.",
        "trigger_text": None,
        "product_name": "부스터 팩 BT06",
        "ip": "승리의 여신: 니케",
    }
    return {**base, **overrides}


class TestCardCatalogue:
    def test_stores_every_field(self, scraper):
        scraper.upsert_card("1", details(), "BT06-001.jpg")
        row = scraper._conn.execute(
            "SELECT number, set_code, name, type, type_en, element, element_en,"
            " cost, power, hit, rarity, affiliation, keywords, effect, product_name, ip,"
            " image_filename FROM cards WHERE wr_id = '1'"
        ).fetchone()
        assert row == (
            "BT06-001",
            "BT06",
            "누아르",
            "유닛",
            "unit",
            "화염",
            "fire",
            2,
            2000,
            1,
            "R",
            ["이펙트", "테트라"],
            ["엔트리"],
            "[엔트리] 이 턴이 끝날 때까지 조우 유닛의 파워 -3000.",
            "부스터 팩 BT06",
            "승리의 여신: 니케",
            "BT06-001.jpg",
        )

    def test_nulls_survive_the_round_trip(self, scraper):
        scraper.upsert_card("1", details(power=None, hit=None, affiliation=[], keywords=[]))
        row = scraper._conn.execute(
            "SELECT power, hit, affiliation, keywords, image_filename FROM cards WHERE wr_id = '1'"
        ).fetchone()
        assert row == (None, None, [], [], None)

    def test_reupsert_refreshes_the_row(self, scraper):
        scraper.upsert_card("1", details(), "BT06-001.jpg")
        scraper.upsert_card("1", details(power=3000, effect="새 효과."), "BT06-001.jpg")
        rows = scraper._conn.execute("SELECT power, effect FROM cards").fetchall()
        assert rows == [(3000, "새 효과.")]

    def test_backfill_does_not_blank_a_known_filename(self, scraper):
        scraper.upsert_card("1", details(), "BT06-001.jpg")
        scraper.upsert_card("1", details(), None)  # a backfill that knows no filename
        stored = scraper._conn.execute("SELECT image_filename FROM cards WHERE wr_id = '1'").fetchone()
        assert stored == ("BT06-001.jpg",)

    def test_variants_share_a_number_without_colliding(self, scraper):
        scraper.upsert_card("1", details(rarity="UR"), "BT06-001.jpg")
        scraper.upsert_card("2", details(rarity="SPR"), "BT06-001-01.jpg")
        rarities = scraper._conn.execute(
            "SELECT rarity FROM cards WHERE number = 'BT06-001' ORDER BY rarity"
        ).fetchall()
        assert rarities == [("SPR",), ("UR",)]

    def test_init_is_idempotent(self, scraper):
        scraper.upsert_card("1", details())
        scraper._init_db()
        assert scraper._conn.execute("SELECT count(*) FROM cards").fetchone() == (1,)

    @pytest.mark.parametrize("missing", ["card_number", "name"])
    def test_a_row_the_app_would_reject_is_skipped(self, scraper, missing):
        # number and name are NOT NULL in the app's schema; a half-parsed card
        # is dropped rather than failing the insert mid-scrape.
        assert scraper.upsert_card("1", details(**{missing: None})) is False
        assert scraper._conn.execute("SELECT count(*) FROM cards").fetchone() == (0,)


class TestCatalogueTableCheck:
    """`cards` belongs to the tracker app's migrations, so it is verified."""

    def test_missing_table_is_a_clear_error(self, tmp_path, monkeypatch, database_url_without_cards):
        monkeypatch.setattr(main.time, "sleep", lambda _: None)
        with pytest.raises(main.CatalogueTableMissing, match="db-migrate"):
            NivelArenaScraper(
                "http://example.test",
                "cardlists",
                database_url=database_url_without_cards,
                downloads_dir=tmp_path / "downloads",
                obey_robots=False,
            )

    def test_stale_table_names_the_missing_columns(self, tmp_path, monkeypatch, database_url):
        import psycopg

        with psycopg.connect(database_url, connect_timeout=5, autocommit=True) as conn:
            conn.execute("ALTER TABLE cards DROP COLUMN keywords, DROP COLUMN ip")

        monkeypatch.setattr(main.time, "sleep", lambda _: None)
        with pytest.raises(main.CatalogueTableMissing, match="ip, keywords"):
            NivelArenaScraper(
                "http://example.test",
                "cardlists",
                database_url=database_url,
                downloads_dir=tmp_path / "downloads",
                obey_robots=False,
            )


class TestBackfillMetadata:
    @staticmethod
    def _stub_endpoint(scraper, monkeypatch, seen=None):
        def fake_details(wr_id):
            if seen is not None:
                seen.append(wr_id)
            return main.BeautifulSoup(
                f"<h2 id='subject'>카드 {wr_id}</h2><h2 id='type'>BT06-00{wr_id} / 유닛 / 화염</h2>",
                "html.parser",
            )

        monkeypatch.setattr(scraper, "get_card_details", fake_details)

    def test_dry_run_makes_no_requests_and_writes_nothing(self, scraper, monkeypatch):
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001.jpg")

        def explode(wr_id):
            raise AssertionError("a dry run must not hit the network")

        monkeypatch.setattr(scraper, "get_card_details", explode)
        assert scraper.backfill_metadata(dry_run=True) == (1, 0)
        assert scraper._conn.execute("SELECT count(*) FROM cards").fetchone() == (0,)

    def test_fills_only_the_rows_that_are_missing(self, scraper, monkeypatch):
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001.jpg")
        scraper.mark_as_scraped("2", "BT06-002", "BT06-002.jpg")
        scraper.upsert_card("1", details(), "BT06-001.jpg")

        seen = []
        self._stub_endpoint(scraper, monkeypatch, seen)
        assert scraper.backfill_metadata(dry_run=False) == (1, 0)
        assert seen == ["2"]

    def test_carries_the_known_filename_across(self, scraper, monkeypatch):
        scraper.mark_as_scraped("2", "BT06-002", "BT06-002-01.jpg")
        self._stub_endpoint(scraper, monkeypatch)
        scraper.backfill_metadata(dry_run=False)
        stored = scraper._conn.execute("SELECT image_filename, name FROM cards WHERE wr_id = '2'").fetchone()
        assert stored == ("BT06-002-01.jpg", "카드 2")

    def test_force_refreshes_rows_that_already_have_metadata(self, scraper, monkeypatch):
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001.jpg")
        scraper.upsert_card("1", details(name="오래된 이름"), "BT06-001.jpg")

        self._stub_endpoint(scraper, monkeypatch)
        assert scraper.backfill_metadata(dry_run=False, force=True) == (1, 0)
        assert scraper._conn.execute("SELECT name FROM cards WHERE wr_id = '1'").fetchone() == ("카드 1",)

    def test_limit_caps_the_run(self, scraper, monkeypatch):
        for wr_id in ("1", "2", "3"):
            scraper.mark_as_scraped(wr_id, f"BT06-00{wr_id}", f"BT06-00{wr_id}.jpg")

        seen = []
        self._stub_endpoint(scraper, monkeypatch, seen)
        assert scraper.backfill_metadata(dry_run=False, limit=2) == (2, 0)
        assert seen == ["1", "2"]

    def test_a_failing_card_does_not_stop_the_run(self, scraper, monkeypatch):
        for wr_id in ("1", "2"):
            scraper.mark_as_scraped(wr_id, f"BT06-00{wr_id}", f"BT06-00{wr_id}.jpg")

        def flaky(wr_id):
            if wr_id == "1":
                raise requests.ConnectionError("transient")
            return main.BeautifulSoup(
                "<h2 id='subject'>카드 2</h2><h2 id='type'>BT06-002 / 유닛 / 화염</h2>", "html.parser"
            )

        monkeypatch.setattr(scraper, "get_card_details", flaky)
        assert scraper.backfill_metadata(dry_run=False) == (1, 1)

    def test_gives_up_after_consecutive_failures(self, scraper, monkeypatch):
        for wr_id in "12345678":
            scraper.mark_as_scraped(wr_id, f"BT06-00{wr_id}", f"BT06-00{wr_id}.jpg")

        attempts = []

        def always_fails(wr_id):
            attempts.append(wr_id)
            raise requests.ConnectionError("down")

        monkeypatch.setattr(scraper, "get_card_details", always_fails)
        assert scraper.backfill_metadata(dry_run=False) == (0, main.MAX_CONSECUTIVE_PAGE_FAILURES)
        assert len(attempts) == main.MAX_CONSECUTIVE_PAGE_FAILURES

    def test_nothing_to_do_is_not_an_error(self, scraper):
        assert scraper.backfill_metadata(dry_run=False) == (0, 0)


class TestScrapeCardStoresMetadata:
    @staticmethod
    def _detail_html():
        return (
            "<h2 id='subject'>누아르</h2><h2 id='type'>BT06-001 / 유닛 / 화염</h2>"
            "<table><tr><td class='h3'>코스트</td><td>2</td>"
            "<td class='h3'>레어도</td><td>R</td></tr></table>"
        )

    def test_metadata_lands_alongside_the_image(self, scraper, monkeypatch):
        monkeypatch.setattr(
            scraper, "get_card_details", lambda wr_id: main.BeautifulSoup(self._detail_html(), "html.parser")
        )
        stub_get(scraper, FakeResponse())
        scraper.scrape_card("1", "remote.jpg")

        assert scraper.is_already_scraped("1")
        row = scraper._conn.execute(
            "SELECT number, name, cost, rarity, image_filename FROM cards WHERE wr_id = '1'"
        ).fetchone()
        assert row == ("BT06-001", "누아르", 2, "R", "BT06-001.jpg")

    def test_unparseable_details_still_download_the_image(self, scraper, monkeypatch):
        monkeypatch.setattr(
            scraper, "get_card_details", lambda wr_id: main.BeautifulSoup("<html></html>", "html.parser")
        )
        stub_get(scraper, FakeResponse())
        scraper.scrape_card("1", "remote.jpg")

        assert scraper.is_already_scraped("1")
        stored = scraper._conn.execute("SELECT card_id FROM scraped_cards WHERE wr_id = '1'").fetchone()
        assert stored == ("unknown_1",)

    def test_a_failed_download_stores_no_metadata(self, scraper, monkeypatch):
        monkeypatch.setattr(
            scraper, "get_card_details", lambda wr_id: main.BeautifulSoup(self._detail_html(), "html.parser")
        )
        stub_get(scraper, FakeResponse(body=b"<html>404</html>"))
        scraper.scrape_card("1", "remote.jpg")

        assert not scraper.is_already_scraped("1")
        assert scraper._conn.execute("SELECT count(*) FROM cards").fetchone() == (0,)


class TestImportSqliteHistory:
    @staticmethod
    def _legacy_db(path, rows):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE scraped_cards (wr_id TEXT PRIMARY KEY, card_id TEXT, image_filename TEXT)")
        conn.executemany("INSERT INTO scraped_cards VALUES (?, ?, ?)", rows)
        conn.commit()
        conn.close()
        return path

    def test_imports_rows(self, scraper, tmp_path):
        legacy = self._legacy_db(tmp_path / "old.db", [("1", "BT06-001", "BT06-001.jpg")])
        assert scraper.import_sqlite_history(legacy, dry_run=False) == 1
        assert scraper.is_already_scraped("1")

    def test_dry_run_writes_nothing(self, scraper, tmp_path):
        legacy = self._legacy_db(tmp_path / "old.db", [("1", "BT06-001", "BT06-001.jpg")])
        assert scraper.import_sqlite_history(legacy, dry_run=True) == 1
        assert not scraper.is_already_scraped("1")

    def test_existing_rows_are_left_alone(self, scraper, tmp_path):
        scraper.mark_as_scraped("1", "BT06-001", "BT06-001-01.jpg")
        legacy = self._legacy_db(tmp_path / "old.db", [("1", "BT06-001", "BT06-001.jpg")])

        assert scraper.import_sqlite_history(legacy, dry_run=False) == 0

        row = scraper._conn.execute("SELECT image_filename FROM scraped_cards WHERE wr_id = '1'").fetchone()
        assert row[0] == "BT06-001-01.jpg"

    def test_missing_file_raises(self, scraper, tmp_path):
        with pytest.raises(FileNotFoundError):
            scraper.import_sqlite_history(tmp_path / "nope.db", dry_run=True)


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
