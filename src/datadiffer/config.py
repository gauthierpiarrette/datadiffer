"""datadiffer.toml, the ONE config file shared by CLI, MCP server, and Action.

Discovery order (explicit wins): $DATADIFFER_CONFIG -> ./datadiffer.toml ->
~/.config/datadiffer/config.toml. ``${ENV_VAR}`` interpolation on string
values keeps secrets out of the file itself.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # tomllib landed in 3.11; tomli is the identical backport
    import tomli as tomllib

from datadiffer.errors import DatadifferError

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

SCAFFOLD = """\
# datadiffer configuration, used by the CLI (conn::table locators),
# the MCP server, and the GitHub Action.
# Secrets belong in environment variables, referenced as ${VAR_NAME}.

# [connections.prod]
# type = "postgres"
# dsn = "postgresql://datadiffer_ro:${PGPASSWORD}@db.internal:5432/analytics"

# [connections.local]
# type = "duckdb"
# path = "./dev.duckdb"

# [connections.wh]
# type = "snowflake"
# account = "ORGNAME-ACCOUNTNAME"
# user = "DATADIFFER_RO"
# private_key_path = "~/.ssh/datadiffer_snowflake_key.p8"
# warehouse = "DIFF_XS"
# database = "ANALYTICS"
# schema = "PUBLIC"          # optional, default PUBLIC
"""


def config_path() -> Path | None:
    env = os.environ.get("DATADIFFER_CONFIG")
    if env:
        return Path(env)
    for p in (Path("datadiffer.toml"), Path.home() / ".config/datadiffer/config.toml"):
        if p.exists():
            return p
    return None


def load_config() -> tuple[dict, Path | None]:
    """Returns the RAW config (no env interpolation). Interpolation is lazy,
    per connection: one unset ${VAR} on connection A must not brick verbs
    that only touch connection B."""
    path = config_path()
    if path is None:
        return {}, None
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise DatadifferError(f"Cannot read config {path}: {e}") from e
    return data, path


def interpolate_conn(conn: dict, context: str = "connection") -> dict:
    """Resolve ${VAR} in ONE connection's subtree; unset vars fail loudly, because
    silently substituting "" turns a missing password into a mystifying auth
    failure downstream."""
    missing: set[str] = set()
    resolved = _interpolate(conn, missing)
    if missing:
        names = ", ".join(f"${{{v}}}" for v in sorted(missing))
        raise DatadifferError(
            f"{context} references {names} but the variable(s) are not set "
            "in the environment"
        )
    return resolved


def connection(name: str) -> dict:
    cfg, path = load_config()
    conns = cfg.get("connections", {})
    if name not in conns:
        available = ", ".join(sorted(conns)) or "(none)"
        where = f" in {path}" if path else " (no datadiffer.toml found; run `datadiffer init`)"
        raise DatadifferError(
            f"Unknown connection {name!r}{where}. Available: {available}"
        )
    return interpolate_conn(conns[name], f"connection {name!r}")


def write_scaffold(path: Path) -> None:
    if path.exists():
        raise DatadifferError(f"{path} already exists, not overwriting.")
    path.write_text(SCAFFOLD)
    os.chmod(path, 0o600)


def conn_label(c: dict) -> str:
    from datadiffer.connect.postgres_ import redact

    try:
        c = interpolate_conn(c)
    except DatadifferError as e:
        return f"(unresolved: {e})"
    if c.get("type") == "postgres":
        return redact(c.get("dsn", ""))
    if c.get("type") == "snowflake":
        return c.get("account", "")
    return c.get("path", "")


def probe_connection(c: dict) -> tuple[bool, str]:
    """Reachability probe. Uses the same quoting as the real diff path so the
    two can never diverge; failure text is credential-scrubbed."""
    import duckdb

    from datadiffer.connect.duckdb_ import _sqlstr
    from datadiffer.connect.postgres_ import redact, redact_text

    try:
        c = interpolate_conn(c)
    except DatadifferError as e:
        return False, str(e)
    ctype = c.get("type")
    if ctype == "snowflake":
        try:
            from datadiffer.connect.snowflake_ import probe_snowflake
            probe_snowflake(c)
            return True, c.get("account", "")
        except DatadifferError as e:
            return False, str(e)
    if ctype == "postgres":
        dsn = c.get("dsn", "")
        con = duckdb.connect(":memory:")
        try:
            con.execute(f"ATTACH {_sqlstr(dsn)} AS probe (TYPE postgres, READ_ONLY)")
            return True, redact(dsn)
        except duckdb.Error as e:
            return False, redact_text(str(e).split("\n")[0], dsn)
        finally:
            con.close()
    if ctype == "duckdb":
        try:
            duckdb.connect(c.get("path", ""), read_only=True).close()
            return True, c.get("path", "")
        except duckdb.Error as e:
            return False, str(e).split("\n")[0]
    return False, f"unsupported type {ctype!r}"


def _interpolate(value, missing: set[str]):
    if isinstance(value, dict):
        return {k: _interpolate(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, missing) for v in value]
    if isinstance(value, str):
        def sub(m):
            name = m.group(1)
            if name not in os.environ:
                missing.add(name)
                return ""
            return os.environ[name]
        return _ENV_REF.sub(sub, value)
    return value
