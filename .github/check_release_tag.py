"""The publish guard: a pushed tag publishes `coxswain` to PyPI only when it names the version the checkout declares.

The tag must be `v` + the root pyproject.toml's `[project] version`, and that version a CalVer release (`YYYY.M.N`,
month unpadded). The rule itself lives in src/tools/agent_tools/release_version.py, loaded from its file so this
check needs nothing installed.

Usage: python .github/check_release_tag.py <tag> [root]. Exits 1, naming the mismatch, on a refusal.
"""
import importlib.util
import pathlib
import sys
import tomllib


def _release_version(root: pathlib.Path):
    path = root / "src" / "tools" / "agent_tools" / "release_version.py"
    spec = importlib.util.spec_from_file_location("release_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refusal(tag: str, version: str, rules) -> str | None:
    """None when `tag` is `v<version>` and `version` is CalVer; else the sentence the publish job fails with."""
    if not rules.is_calver(version) or version != rules.bare(version):
        return f"pyproject.toml version {version!r} is not a CalVer release (YYYY.M.N)"
    if tag != rules.release_tag(version):
        return f"tag {tag!r} does not name pyproject.toml's version {version} (expected {rules.release_tag(version)})"
    return None


def main(argv: list[str]) -> int:
    tag, root = argv[0], pathlib.Path(argv[1] if len(argv) > 1 else ".")
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    reason = refusal(tag, version, _release_version(root))
    if reason:
        print(f"refusing to publish: {reason}", file=sys.stderr)
        return 1
    print(f"publishing coxswain {version} for {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
