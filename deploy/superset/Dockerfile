FROM apache/superset:4.1.2

USER root
# /var/lib/superset holds the metadata volume. It lives outside /app/superset_home
# so the volume never shadows the superset user's ~/.duckdb extension cache.
# `python -m pip` installs into the interpreter `python` resolves to, the one that
# runs superset. An image whose venv lacks pip fails here, at build, not at query time.
RUN mkdir -p /var/lib/superset \
    && chown superset:superset /var/lib/superset \
    && python -m pip install --no-cache-dir duckdb==1.1.3 duckdb-engine==0.13.5

# Extensions install into the running user's home, so this runs as superset,
# not root. Root's ~/.duckdb is invisible to the process that serves queries.
USER superset
RUN python -c "import duckdb; [duckdb.sql(f'INSTALL {e}') for e in ('sqlite','parquet','iceberg','postgres')]"
# The build is the evidence: the duckdb-engine dialect must load in Superset's interpreter.
RUN python -c "import sqlalchemy; sqlalchemy.create_engine('duckdb:///:memory:').connect().close()"
