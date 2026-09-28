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


# UNVERIFIED: catalog_name, table_namespace, table_name and metadata_location are pyiceberg's SqlCatalog
# columns. docs/releases/0.17.0.md confirms the lake uses "pyiceberg with a SQL catalog", but this checkout
# vendors no pyiceberg source (`git grep -il pyiceberg` finds only docs, uv.lock and this file) and uv.lock
# pins `pyiceberg>=0.9` for the optional `lake` extra with no resolved package block to read a model from,
# so the column names themselves are not confirmed here. The chair confirms them against the live store on
# the first Docker run; a wrong name fails every DuckDB connect, loudly, rather than silently dropping the lake.
LAKE_TABLES_SQL = (
    "SELECT table_name, metadata_location FROM store.public.iceberg_tables "
    "WHERE catalog_name = 'coxswain' AND table_namespace = 'coxswain'"
)


def _quote(value: str) -> str:
    """Double a single quote so `value` is safe inside a single-quoted SQL literal."""
    return value.replace(chr(39), chr(39) * 2)


def attach_sql(store_url: str | None) -> str | None:
    """The ATTACH statement for a Postgres store URL; None for anything else, which leaves cox.db in use."""
    if not store_url or urllib.parse.urlparse(store_url).scheme not in ("postgres", "postgresql"):
        return None
    # The catalog name `store` must equal bootstrap.STORE_CATALOG; this file is mounted alone and cannot import it.
    return f"ATTACH '{_quote(store_url)}' AS store (TYPE postgres, READ_ONLY)"


def lake_secret_sql(env: dict) -> str | None:
    """The CREATE SECRET statement for the Garage S3 endpoint from an environment mapping; None when
    COXSWAIN_S3_ENDPOINT is unset or empty, which skips loading the lake entirely."""
    endpoint = env.get("COXSWAIN_S3_ENDPOINT") or ""
    if not endpoint:
        return None
    key_id = env.get("COXSWAIN_S3_KEY_ID") or ""
    secret = env.get("COXSWAIN_S3_SECRET") or ""
    region = env.get("COXSWAIN_S3_REGION") or "garage"
    return (
        "CREATE OR REPLACE SECRET lake_s3 (TYPE s3, "
        f"KEY_ID '{_quote(key_id)}', SECRET '{_quote(secret)}', REGION '{_quote(region)}', "
        f"ENDPOINT '{_quote(endpoint)}', URL_STYLE 'path', USE_SSL false)"
    )


def lake_view_sql(table: str, metadata_location: str) -> str:
    """The CREATE OR REPLACE VIEW statement that scans one Iceberg table's current metadata JSON.
    `lake` here must equal bootstrap.LAKE_SCHEMA (defined there as "lake"); this file cannot import bootstrap.py."""
    return f"CREATE OR REPLACE VIEW lake.{table} AS SELECT * FROM iceberg_scan('{_quote(metadata_location)}')"


def _attach_lake(dbapi_connection, env) -> None:
    """Load the S3/Iceberg extensions, create the Garage secret, and recreate every lake view from the
    store's Iceberg catalog; a no-op when COXSWAIN_S3_ENDPOINT is unset. The catalog read and the
    per-table loop are the impure edge; lake_secret_sql and lake_view_sql do the SQL building."""
    secret = lake_secret_sql(env)
    if secret is None:
        return
    dbapi_connection.execute("LOAD httpfs")
    dbapi_connection.execute("LOAD iceberg")
    dbapi_connection.execute(secret)
    for table, metadata_location in dbapi_connection.execute(LAKE_TABLES_SQL).fetchall():
        dbapi_connection.execute(lake_view_sql(table, metadata_location))


def attach_store(dbapi_connection, connection_record) -> None:
    """Attach the Postgres store, then the Garage-backed Iceberg lake, to each new DuckDB connection;
    other engines, such as Superset's SQLite, are left alone."""
    if not type(dbapi_connection).__module__.startswith("duckdb"):
        return
    statement = attach_sql(os.environ.get("COXSWAIN_STORE_URL"))
    if statement is None:
        return
    dbapi_connection.execute("LOAD postgres")
    dbapi_connection.execute(statement)
    _attach_lake(dbapi_connection, os.environ)


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
