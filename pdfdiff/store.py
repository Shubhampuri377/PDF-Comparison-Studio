"""SQLite-backed result store.

The whole point of this module is that a comparison of 20,000 page documents
must never hold 20,000 pages of anything in RAM.  Page payloads are compressed
and written to disk during ingest; the diff phase streams them back in chunks;
the UI queries aggregates and windows of rows.

The database is also what makes runs resumable and workers cheap: a worker gets
a path, not a pickled 200 MB payload.
"""
from __future__ import annotations

import os
import pickle
import sqlite3
import threading
import zlib
from typing import Iterator, Sequence

from .models import (
    ALL_CATEGORIES, FLAGS_KEY, KIND_ADDED, KIND_REMOVED, PageStat, ST_CHANGED,
    ST_ERROR, ST_IDENTICAL, ST_PENDING, SCHEMA_VERSION, Summary,
)

_COMPRESS_LEVEL = 6

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS doc_page (
    side       INTEGER NOT NULL,
    pno        INTEGER NOT NULL,
    width      REAL    NOT NULL,
    height     REAL    NOT NULL,
    rotation   INTEGER NOT NULL DEFAULT 0,
    n_words    INTEGER NOT NULL DEFAULT 0,
    n_images   INTEGER NOT NULL DEFAULT 0,
    exact_hash INTEGER NOT NULL DEFAULT 0,
    simhash    INTEGER NOT NULL DEFAULT 0,
    payload    BLOB,
    PRIMARY KEY (side, pno)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS pair (
    idx        INTEGER PRIMARY KEY,
    a_pno      INTEGER,
    b_pno      INTEGER,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    similarity REAL NOT NULL DEFAULT 1.0,
    n_total    INTEGER NOT NULL DEFAULT 0,
    n_text     INTEGER NOT NULL DEFAULT 0,
    n_image    INTEGER NOT NULL DEFAULT 0,
    counts     BLOB,
    boxes      BLOB,
    error      TEXT
);

CREATE INDEX IF NOT EXISTS pair_status_idx ON pair(status, idx);
CREATE INDEX IF NOT EXISTS pair_kind_idx   ON pair(kind, idx);
CREATE INDEX IF NOT EXISTS pair_text_idx   ON pair(n_text, idx);
CREATE INDEX IF NOT EXISTS pair_image_idx  ON pair(n_image, idx);
"""


def pack(obj) -> bytes:
    return zlib.compress(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL),
                         _COMPRESS_LEVEL)


def unpack(blob: bytes | None):
    if not blob:
        return None
    return pickle.loads(zlib.decompress(blob))


# Filters exposed by the UI's page list.
FILTER_ALL = "all"
FILTER_CHANGED = "changed"
FILTER_IDENTICAL = "identical"
FILTER_TEXT = "text"
FILTER_IMAGE = "image"
FILTER_ADDED = "added"
FILTER_REMOVED = "removed"
FILTER_ERROR = "error"

_FILTER_SQL = {
    FILTER_ALL: "",
    FILTER_CHANGED: "WHERE status = 'changed' OR kind != 'matched'",
    FILTER_IDENTICAL: "WHERE status = 'identical'",
    FILTER_TEXT: "WHERE n_text > 0",
    FILTER_IMAGE: "WHERE n_image > 0",
    FILTER_ADDED: "WHERE kind = 'added'",
    FILTER_REMOVED: "WHERE kind = 'removed'",
    FILTER_ERROR: "WHERE status = 'error'",
}


class ResultStore:
    """Owns one SQLite file.

    One :class:`ResultStore` per thread.  SQLite connections are not shareable
    across threads by default and the UI, the engine and the render workers all
    read concurrently -- each gets its own instance pointed at the same path.
    """

    def __init__(self, path: str, readonly: bool = False):
        self.path = path
        self.readonly = readonly
        self._local = threading.local()
        if not readonly:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with self._connect() as con:
                con.executescript(SCHEMA)

    # -- connection handling ------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is not None:
            return con
        if self.readonly:
            uri = f"file:{self.path.replace('?', '%3f').replace('#', '%23')}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=30.0)
        else:
            con = sqlite3.connect(self.path, timeout=30.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        # 64 MB page cache: big enough to keep the pair table hot at 20k rows,
        # small enough to stay invisible next to the render cache.
        con.execute("PRAGMA cache_size=-65536")
        con.execute("PRAGMA busy_timeout=30000")
        self._local.con = con
        return con

    def close(self):
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- meta ---------------------------------------------------------------
    def set_meta(self, key: str, value: str):
        con = self._connect()
        with con:
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        (key, value))

    def get_meta(self, key: str, default=None):
        row = self._connect().execute(
            "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta_many(self, mapping: dict):
        con = self._connect()
        with con:
            con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                            list(mapping.items()))

    def init_schema_version(self):
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def schema_ok(self) -> bool:
        return self.get_meta("schema_version") == str(SCHEMA_VERSION)

    # -- ingest -------------------------------------------------------------
    def write_pages(self, rows: Sequence[tuple]):
        """``rows`` are ``(side, pno, w, h, rot, n_words, n_images,
        exact_hash, simhash, payload_blob)``."""
        con = self._connect()
        with con:
            con.executemany(
                "INSERT OR REPLACE INTO doc_page "
                "(side, pno, width, height, rotation, n_words, n_images, "
                " exact_hash, simhash, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def ingested_pages(self, side: int) -> int:
        row = self._connect().execute(
            "SELECT COUNT(*) FROM doc_page WHERE side = ?", (side,)).fetchone()
        return row[0] if row else 0

    def ingested_page_numbers(self, side: int) -> set[int]:
        cur = self._connect().execute(
            "SELECT pno FROM doc_page WHERE side = ?", (side,))
        return {r[0] for r in cur}

    def page_geometry(self, side: int, pno: int) -> tuple[float, float, int] | None:
        row = self._connect().execute(
            "SELECT width, height, rotation FROM doc_page WHERE side=? AND pno=?",
            (side, pno)).fetchone()
        return tuple(row) if row else None

    def page_payload(self, side: int, pno: int):
        row = self._connect().execute(
            "SELECT payload FROM doc_page WHERE side=? AND pno=?",
            (side, pno)).fetchone()
        return unpack(row[0]) if row else None

    def page_payloads(self, keys: Sequence[tuple[int, int]]) -> dict:
        """Bulk fetch; one query per side keeps the diff workers I/O-light."""
        if not keys:
            return {}
        out = {}
        by_side: dict[int, list[int]] = {}
        for side, pno in keys:
            by_side.setdefault(side, []).append(pno)
        con = self._connect()
        for side, pnos in by_side.items():
            # Chunk to stay clear of SQLite's variable limit.
            for i in range(0, len(pnos), 400):
                block = pnos[i:i + 400]
                qs = ",".join("?" * len(block))
                cur = con.execute(
                    f"SELECT pno, width, height, rotation, payload "
                    f"FROM doc_page WHERE side=? AND pno IN ({qs})",
                    (side, *block))
                for pno, w, h, rot, payload in cur:
                    out[(side, pno)] = (w, h, rot, unpack(payload))
        return out

    def fingerprints(self, side: int) -> tuple[list[int], list[int], list[int]]:
        """Return ``(exact_hashes, simhashes, token_counts)`` ordered by page."""
        cur = self._connect().execute(
            "SELECT exact_hash, simhash, n_words FROM doc_page "
            "WHERE side=? ORDER BY pno", (side,))
        exact: list[int] = []
        sims: list[int] = []
        counts: list[int] = []
        for e, s, n in cur:
            exact.append(e)
            sims.append(s)
            counts.append(n)
        return exact, sims, counts

    # -- pairs --------------------------------------------------------------
    def write_pairs(self, pairs: Sequence[tuple[int, int | None, int | None, str]]):
        con = self._connect()
        with con:
            con.execute("DELETE FROM pair")
            con.executemany(
                "INSERT INTO pair(idx, a_pno, b_pno, kind, status) "
                "VALUES (?,?,?,?,'pending')", pairs)

    def reset_pair_results(self):
        con = self._connect()
        with con:
            con.execute(
                "UPDATE pair SET status='pending', similarity=1.0, n_total=0, "
                "n_text=0, n_image=0, counts=NULL, boxes=NULL, error=NULL")

    def write_pair_results(self, rows: Sequence[tuple]):
        """``rows`` are ``(status, similarity, n_total, n_text, n_image,
        counts_blob, boxes_blob, error, idx)``."""
        con = self._connect()
        with con:
            con.executemany(
                "UPDATE pair SET status=?, similarity=?, n_total=?, n_text=?, "
                "n_image=?, counts=?, boxes=?, error=? WHERE idx=?", rows)

    def pending_pair_indices(self) -> list[int]:
        cur = self._connect().execute(
            "SELECT idx FROM pair WHERE status='pending' ORDER BY idx")
        return [r[0] for r in cur]

    def pair_plan(self, indices: Sequence[int]) -> list[tuple]:
        if not indices:
            return []
        out = []
        con = self._connect()
        for i in range(0, len(indices), 400):
            block = list(indices[i:i + 400])
            qs = ",".join("?" * len(block))
            cur = con.execute(
                f"SELECT idx, a_pno, b_pno, kind FROM pair WHERE idx IN ({qs})",
                block)
            out.extend(cur.fetchall())
        out.sort(key=lambda r: r[0])
        return out

    def pair_count(self, flt: str = FILTER_ALL) -> int:
        sql = f"SELECT COUNT(*) FROM pair {_FILTER_SQL.get(flt, '')}"
        return self._connect().execute(sql).fetchone()[0]

    def pair_indices(self, flt: str = FILTER_ALL) -> list[int]:
        """The ordered index list backing the page navigator.

        Even at 20k pages this is a list of ints -- about 160 KB -- which is
        what lets the list view stay virtual while filtering stays instant.
        """
        sql = f"SELECT idx FROM pair {_FILTER_SQL.get(flt, '')} ORDER BY idx"
        return [r[0] for r in self._connect().execute(sql)]

    def _row_to_stat(self, row) -> PageStat:
        (idx, a, b, kind, status, sim, n_total, n_text, n_image,
         counts, error) = row
        raw = unpack(counts) or {}
        flags = raw.get(FLAGS_KEY) or []
        clean = {k: v for k, v in raw.items() if not k.startswith("_")}
        return PageStat(idx=idx, a_pno=a, b_pno=b, kind=kind, status=status,
                        counts=clean, similarity=sim, error=error,
                        flags=list(flags))

    def get_stat(self, idx: int) -> PageStat | None:
        row = self._connect().execute(
            "SELECT idx, a_pno, b_pno, kind, status, similarity, n_total, "
            "n_text, n_image, counts, error FROM pair WHERE idx=?",
            (idx,)).fetchone()
        return self._row_to_stat(row) if row else None

    def get_stats(self, indices: Sequence[int]) -> dict[int, PageStat]:
        if not indices:
            return {}
        out: dict[int, PageStat] = {}
        con = self._connect()
        idx_list = list(indices)
        for i in range(0, len(idx_list), 400):
            block = idx_list[i:i + 400]
            qs = ",".join("?" * len(block))
            cur = con.execute(
                f"SELECT idx, a_pno, b_pno, kind, status, similarity, n_total, "
                f"n_text, n_image, counts, error FROM pair WHERE idx IN ({qs})",
                block)
            for row in cur:
                out[row[0]] = self._row_to_stat(row)
        return out

    def iter_stats(self, flt: str = FILTER_ALL,
                   batch: int = 500) -> Iterator[PageStat]:
        """Stream every matching row without materialising the whole set."""
        sql = (f"SELECT idx, a_pno, b_pno, kind, status, similarity, n_total, "
               f"n_text, n_image, counts, error FROM pair "
               f"{_FILTER_SQL.get(flt, '')} ORDER BY idx")
        cur = self._connect().execute(sql)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            for row in rows:
                yield self._row_to_stat(row)

    def get_boxes(self, idx: int) -> list[tuple]:
        row = self._connect().execute(
            "SELECT boxes FROM pair WHERE idx=?", (idx,)).fetchone()
        return unpack(row[0]) if row and row[0] else []

    def next_changed(self, after_idx: int, forward: bool = True) -> int | None:
        con = self._connect()
        if forward:
            row = con.execute(
                "SELECT idx FROM pair WHERE idx > ? AND "
                "(status='changed' OR kind != 'matched') ORDER BY idx LIMIT 1",
                (after_idx,)).fetchone()
        else:
            row = con.execute(
                "SELECT idx FROM pair WHERE idx < ? AND "
                "(status='changed' OR kind != 'matched') "
                "ORDER BY idx DESC LIMIT 1", (after_idx,)).fetchone()
        return row[0] if row else None

    # -- rollup -------------------------------------------------------------
    def summary(self) -> Summary:
        con = self._connect()
        s = Summary()
        row = con.execute(
            "SELECT COUNT(*),"
            " SUM(status='identical'),"
            " SUM(status='changed'),"
            " SUM(kind='added'),"
            " SUM(kind='removed'),"
            " SUM(status='error') FROM pair").fetchone()
        if row:
            s.total_pairs = row[0] or 0
            s.identical = row[1] or 0
            s.changed = row[2] or 0
            s.added_pages = row[3] or 0
            s.removed_pages = row[4] or 0
            s.errors = row[5] or 0
        s.pages_a = self.ingested_pages(0)
        s.pages_b = self.ingested_pages(1)

        # Category totals live inside the packed ``counts`` blob, so they are
        # summed in Python -- but only over rows that actually changed.
        totals = {c: 0 for c in ALL_CATEGORIES}
        cur = con.execute("SELECT counts FROM pair WHERE n_total > 0")
        while True:
            rows = cur.fetchmany(1000)
            if not rows:
                break
            for (blob,) in rows:
                d = unpack(blob) or {}
                for k, v in d.items():
                    if k.startswith("_"):        # flags, not counters
                        continue
                    totals[k] = totals.get(k, 0) + v
        s.counts = {k: v for k, v in totals.items() if v}
        return s

    def vacuum(self):
        self._connect().execute("VACUUM")
