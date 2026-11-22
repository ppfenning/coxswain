"""`store_switch.default_copy_store` on a real Postgres: it runs the real `python -m harness.store_copy`, both URLs in
the child's environment and none in its argv, and the row in the legacy sqlite store reaches the Postgres target.

The installed interpreter does every step; nothing is stubbed.
"""

import json
import subprocess
from pathlib import Path

NOW = "2026-11-22T00:00:00Z"

# argv: the sqlite source URL, then the Postgres target URL. Migrates the legacy source, puts one lease row in it,
# copies it with `default_copy_store`, and prints the counts that copy reported.
COPY = """
import json, os, sys
os.environ["COXSWAIN_STORE_DOUBLE"] = "sqlite"  # the legacy store this switch reads is the sqlite double
from harness import store_chair_lease
from harness.store_migrate import open_store
from agent_tools.store_switch import default_copy_store
src, dst = sys.argv[1], sys.argv[2]
conn = open_store(src, {now!r})
store_chair_lease.acquire(conn, "smoke:copy", "smoke", 60)
conn.close()
print(json.dumps(default_copy_store(src, dst)))
"""


def test_default_copy_store_copies_a_legacy_store_into_postgres_with_its_urls_in_the_environment(sandbox, tmp_path: Path):
    src = f"sqlite:///{tmp_path / 'legacy.db'}"
    done = subprocess.run([str(sandbox.install.python), "-c", COPY.format(now=NOW), src, sandbox.store_url],
                          env=sandbox.env, cwd=sandbox.home, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    counts = json.loads(done.stdout.splitlines()[-1])
    assert counts["leases"] == 1
    assert all(isinstance(n, int) for n in counts.values())
