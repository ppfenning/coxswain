import os

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
