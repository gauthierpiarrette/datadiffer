"""Snowflake sources: key-pair auth, Arrow pull into the local engine.

No DuckDB attach exists for Snowflake, so a source is pulled ONCE (a single
SELECT = a single Snowflake snapshot) into a temp table in our scratch DuckDB;
every diff pass then reads that consistent local copy. Only needed columns are
projected into the pull (excluded columns never cross the
wire). Sessions open with ABORT_DETACHED_QUERY=TRUE and a statement timeout so
an abandoned process can never keep burning credits.

Identifier case: Snowflake uppercases unquoted identifiers. Columns whose
names are entirely uppercase are lowercased for matching (the engine's own
case-insensitive convention); mixed-case (quoted) names are kept verbatim.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import duckdb

from datadiffer.connect.duckdb_ import LocalSource
from datadiffer.connect.postgres_ import BYTE_HARD_CAP, ROW_HARD_CAP, ROW_WARN
from datadiffer.errors import DatadifferError

_SESSION = {"ABORT_DETACHED_QUERY": True, "STATEMENT_TIMEOUT_IN_SECONDS": 1800}


def _load_key_der(path: str) -> bytes:
    from cryptography.hazmat.primitives import serialization

    try:
        with open(os.path.expanduser(path), "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    except (OSError, ValueError) as e:
        raise DatadifferError(f"Cannot load Snowflake private key {path!r}: {e}") from e
    return key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _connect(cfg: dict, timeout_seconds: float | None = None):
    try:
        import snowflake.connector
    except ImportError as e:
        raise DatadifferError(
            "Snowflake support requires the extra: pip install 'datadiffer[snowflake]'"
        ) from e
    session = dict(_SESSION)
    if timeout_seconds:
        import math
        session["STATEMENT_TIMEOUT_IN_SECONDS"] = min(1800, max(1, math.ceil(timeout_seconds)))
    kwargs = {
        "account": cfg.get("account", ""),
        "user": cfg.get("user", ""),
        "warehouse": cfg.get("warehouse"),
        "database": cfg.get("database"),
        "session_parameters": session,
        # Without this, NUMBER(p,s) arrives as float64 and
        # sub-double-resolution changes compare EQUAL, which is a silently missed diff.
        "arrow_number_to_decimal": True,
    }
    if cfg.get("role"):
        kwargs["role"] = cfg["role"]
    if cfg.get("private_key_path"):
        kwargs["private_key"] = _load_key_der(cfg["private_key_path"])
    elif cfg.get("password"):
        kwargs["password"] = cfg["password"]
    else:
        raise DatadifferError(
            "Snowflake connection needs private_key_path (recommended) or password."
        )
    try:
        return snowflake.connector.connect(**kwargs)
    except Exception as e:  # connector raises many error types
        raise DatadifferError(f"Cannot connect to Snowflake: {e}") from e


def probe_snowflake(cfg: dict) -> None:
    con = _connect(cfg)
    try:
        con.cursor().execute("SELECT 1")
    finally:
        con.close()


@dataclass
class SnowflakeHandle:
    """A not-yet-pulled Snowflake table. schema_map() first, then pull()."""

    cfg: dict
    database: str
    schema: str
    table: str
    display: str
    stem: str
    _con: object = None

    @classmethod
    def open(
        cls, cfg: dict, table_ref: str, timeout_seconds: float | None = None
    ) -> SnowflakeHandle:
        if "://" in table_ref or "::" in table_ref:
            raise DatadifferError(f"Bad table reference {table_ref!r}.")
        parts = table_ref.split(".")
        if len(parts) > 3 or not all(parts):
            raise DatadifferError(
                f"Bad table reference {table_ref!r}, use [db.][schema.]table"
            )
        database = parts[0] if len(parts) == 3 else cfg.get("database", "")
        schema = parts[-2] if len(parts) >= 2 else cfg.get("schema", "PUBLIC")
        if not database:
            raise DatadifferError(
                "Snowflake connection has no database and the table reference "
                "does not include one (use db.schema.table)."
            )
        handle = cls(
            cfg=cfg, database=database, schema=schema, table=parts[-1],
            display=f"{table_ref} ({cfg.get('account', '')})", stem=parts[-1].lower(),
        )
        handle._con = _connect(cfg, timeout_seconds)
        return handle

    def _fqn(self) -> str:
        return ".".join(
            _qid(_unfold(p)) for p in (self.database, self.schema, self.table)
        )

    def _cur(self):
        return self._con.cursor()

    def schema_map(self) -> dict[str, str]:
        """{matched_name: snowflake_type}; all-uppercase names lowercased."""
        try:
            rows = self._cur().execute(f"DESCRIBE TABLE {self._fqn()}").fetchall()
        except Exception as e:
            raise DatadifferError(f"Cannot read {self.display}: {e}") from e
        cols: dict[str, str] = {}
        names: dict[str, str] = {}
        for r in rows:
            name, ftype, kind = r[0], r[1], r[2]
            if kind != "COLUMN":
                continue
            folded = _fold(name)
            if folded in names:
                raise DatadifferError(
                    f"{self.display}: columns {names[folded]!r} and {name!r} collide "
                    "after case folding; rename one, ambiguous matches are refused."
                )
            names[folded] = name
            cols[folded] = ftype
        self._names = names
        self._json_cols = {
            _fold(r[0]) for r in rows
            if r[2] == "COLUMN" and r[1].upper().startswith(("VARIANT", "OBJECT", "ARRAY"))
        }
        return cols

    def _estimates(self) -> tuple[int | None, int | None]:
        if not hasattr(self, "_est"):
            cur = self._cur()
            cur.execute(
                "SELECT row_count, bytes FROM identifier(%s) WHERE table_schema = %s "
                "AND table_name = %s",
                (f"{self.database}.information_schema.tables",
                 _unfold(self.schema), _unfold(self.table)),
            )
            row = cur.fetchone()
            self._est = tuple((row or (None, None))[:2])
        return self._est

    def row_estimate(self) -> int | None:
        rows, _ = self._estimates()
        return int(rows) if rows is not None else None

    def byte_estimate(self) -> int | None:
        _, size = self._estimates()
        return int(size) if size is not None else None

    def guard(self) -> list[str]:
        """Row/byte admission caps from table metadata (micro-partition stats)."""
        warnings: list[str] = []
        rows_est, bytes_est = self._estimates()
        if rows_est is None:
            warnings.append(f"row_estimate_unavailable:{self.stem}")
        elif rows_est > ROW_HARD_CAP:
            raise DatadifferError(
                f"{self.display} has ~{rows_est:,} rows, over the "
                f"{ROW_HARD_CAP:,} cross-source cap. Narrow the diff with --where."
            )
        elif rows_est > ROW_WARN:
            warnings.append(f"large_pull:{self.stem}:~{rows_est}")
        if bytes_est is not None and bytes_est > BYTE_HARD_CAP:
            raise DatadifferError(
                f"{self.display} is ~{bytes_est / 1024**3:.1f} GiB, over the "
                f"{BYTE_HARD_CAP / 1024**3:.0f} GiB cross-source cap. "
                "Narrow the diff or exclude wide columns."
            )
        return warnings

    def declared_key_sets(self) -> list[list[str]]:
        out: list[list[str]] = []
        for show in ("SHOW PRIMARY KEYS IN TABLE", "SHOW UNIQUE KEYS IN TABLE"):
            try:
                cur = self._cur()
                cur.execute(f"{show} {self._fqn()}")
                names = [d[0].lower() for d in cur.description]
                rows = [dict(zip(names, r, strict=False)) for r in cur.fetchall()]
            except Exception:
                continue
            by_constraint: dict[str, list] = {}
            for r in rows:
                if "column_name" in r:
                    by_constraint.setdefault(r.get("constraint_name", ""), []).append(r)
            for _, grp in sorted(by_constraint.items()):
                grp.sort(key=lambda r: r.get("key_sequence", 0))
                out.append([_fold(r["column_name"]) for r in grp])
        return out

    def pull(self, duck: duckdb.DuckDBPyConnection, columns: list[str], alias: str) -> LocalSource:
        """One projected SELECT (one Snowflake snapshot) -> local temp table."""
        actual = getattr(self, "_names", {})
        select = ", ".join(
            f"{_qid(actual.get(c, c))} AS {_qid(c)}" for c in columns
        )
        cur = self._cur()
        try:
            import pyarrow as pa

            cur.execute(f"SELECT {select} FROM {self._fqn()}")
            # fetch_arrow_batches yields pa.Table chunks; DuckDB wants a
            # RecordBatchReader streams without materializing the whole pull.
            # Microsecond precision: nanosecond arrivals become TIMESTAMP_NS, which
            # is not comparable against a TIMESTAMP column from another source.
            chunks = cur.fetch_arrow_batches(force_microsecond_precision=True)
            first = next(chunks, None)
            if first is None:
                cur.execute(f"SELECT {select} FROM {self._fqn()} LIMIT 0")
                empty = cur.fetch_arrow_all(
                    force_return_table=True, force_microsecond_precision=True
                )
                reader = pa.RecordBatchReader.from_batches(empty.schema, iter(()))
            else:
                def _batches(first=first, rest=chunks):
                    yield from first.to_batches()
                    for t in rest:
                        yield from t.to_batches()

                reader = pa.RecordBatchReader.from_batches(first.schema, _batches())
            duck.register("__dd_sf_reader", reader)
            # VARIANT/OBJECT/ARRAY arrive as Snowflake's pretty-printed JSON
            # text: re-normalize ONCE locally (CAST -> JSON) so the engine
            # compares canonical JSON on both sides (compared_as = json_text).
            json_here = sorted(
                getattr(self, "_json_cols", set()) & set(columns)
            )
            replace = ""
            if json_here:
                casts = ", ".join(f"CAST({_qid(c)} AS JSON) AS {_qid(c)}" for c in json_here)
                replace = f" REPLACE ({casts})"
            duck.execute(
                f"CREATE OR REPLACE TEMP TABLE {alias} AS "
                f"SELECT *{replace} FROM __dd_sf_reader"
            )
            duck.unregister("__dd_sf_reader")
        except Exception as e:
            raise DatadifferError(f"Snowflake pull failed for {self.display}: {e}") from e
        return LocalSource(
            rel=alias, display=self.display, stem=self.stem, kind="snowflake",
        )

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None


_UNQUOTED = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def _fold(name: str) -> str:
    return name.lower() if name == name.upper() else name


def _unfold(part: str) -> str:
    """Resolve a user-typed identifier the way Snowflake resolves unquoted
    ones: names matching the unquoted grammar fold to UPPER; anything needing
    quotes is treated verbatim."""
    return part.upper() if _UNQUOTED.match(part) else part


def _qid(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
