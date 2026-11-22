"""The harness's launcher and composition root: `python shell.py <graph> ...`.

Every launch goes through here (`cox route launch`, the chair, the worker), so this is where the tools' store and
route calls are built and passed to `harness.cli.main`. The harness itself never imports the tools package
(`test_import_direction`); this file is outside the harness, graphs and runner packages, and may.
"""

from agent_tools.harness_ports import decompose_ports
from harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main(ports=decompose_ports()))
