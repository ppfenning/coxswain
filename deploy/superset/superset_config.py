import contextlib
import os
import urllib.parse

PLACEHOLDER = "change-me"
ADMIN_VARS = (
    "SUPERSET_ADMIN_USERNAME",
    "SUPERSET_ADMIN_PASSWORD",
    "SUPERSET_ADMIN_EMAIL",
    "SUPERSET_ADMIN_FIRSTNAME",
    "SUPERSET_ADMIN_LASTNAME",
)


def _require(name: str) -> str:
    """Read a required variable from the environment; fail fast when unset, empty, or the .env.example placeholder."""
    value = os.environ.get(name, "")
    if not value or value == PLACEHOLDER:
        raise RuntimeError(f"{name} is unset or still {PLACEHOLDER!r}; copy .env.example to .env and fill it in")
    return value


def attach_sql(store_url: str | None) -> str | None:
    """The ATTACH statement for a Postgres store URL; None for anything else, which leaves cox.db in use."""
    if not store_url or urllib.parse.urlparse(store_url).scheme not in ("postgres", "postgresql"):
        return None
    # The catalog name `store` must equal bootstrap.STORE_CATALOG; this file is mounted alone and cannot import it.
    return f"ATTACH '{store_url.replace(chr(39), chr(39) * 2)}' AS store (TYPE postgres, READ_ONLY)"


def attach_store(dbapi_connection, connection_record) -> None:
    """Attach the Postgres store to each new DuckDB connection; other engines, such as Superset's SQLite, are left alone."""
    if not type(dbapi_connection).__module__.startswith("duckdb"):
        return
    statement = attach_sql(os.environ.get("COXSWAIN_STORE_URL"))
    if statement is None:
        return
    dbapi_connection.execute("LOAD postgres")
    dbapi_connection.execute(statement)


def _register() -> None:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    event.listen(Engine, "connect", attach_store)


# Superset always ships SQLAlchemy; only the bare test environment lacks it.
with contextlib.suppress(ModuleNotFoundError):
    _register()


SECRET_KEY = _require("SUPERSET_SECRET_KEY")

# Every variable superset-init passes to create-admin. Read here so a missing one
# stops both services at startup instead of letting create-admin fail on an empty value.
_ADMIN = {name: _require(name) for name in ADMIN_VARS}

# Superset's own metadata, on the named volume mounted at /var/lib/superset.
SQLALCHEMY_DATABASE_URI = "sqlite:////var/lib/superset/superset.db"

# Defaults only. Add a flag here with the reason beside it.
FEATURE_FLAGS: dict[str, bool] = {}

# Superset refuses file-backed engines such as DuckDB unless this is False.
# This is not read-only safety: an admin can also point a connection at the writable
# metadata DB in /var/lib/superset. The accepted risk is one trusted local admin,
# with the port bound to 127.0.0.1 only.
PREVENT_UNSAFE_DB_CONNECTIONS = False
