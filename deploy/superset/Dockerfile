FROM apache/superset:4.1.2

USER root
# /var/lib/superset holds the metadata volume. It lives outside /app/superset_home
# so the volume never shadows the superset user's ~/.duckdb extension cache.
# `python -m pip` installs into the interpreter `python` resolves to, the one that
# runs superset. An image whose venv lacks pip fails here, at build, not at query time.
# duckdb 1.5.5 / duckdb-engine 0.17.0: 1.1.3's iceberg extension cannot scan a table whose
# metadata JSON lives behind an S3-compatible endpoint (Garage) reached through httpfs; the
# same duckdb line is already pinned for cox lake elsewhere in this repo (see uv.lock).
RUN mkdir -p /var/lib/superset \
    && chown superset:superset /var/lib/superset \
    && python -m pip install --no-cache-dir duckdb==1.5.5 duckdb-engine==0.17.0

# Extensions install into the running user's home, so this runs as superset,
# not root. Root's ~/.duckdb is invisible to the process that serves queries.
USER superset
RUN python -c "import duckdb; [duckdb.sql(f'INSTALL {e}') for e in ('sqlite','parquet','iceberg','postgres','httpfs')]"
# The build is the evidence: the duckdb-engine dialect must load in Superset's interpreter.
RUN python -c "import sqlalchemy; sqlalchemy.create_engine('duckdb:///:memory:').connect().close()"
