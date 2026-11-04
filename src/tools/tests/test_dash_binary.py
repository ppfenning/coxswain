from agent_tools import dash_binary as db

LINUX = "x86_64-unknown-linux-gnu"
MAC = "aarch64-apple-darwin"
SPEC = {"repo": "ppfenning/coxswain-dash", "tag": "v2026.11.1"}


def test_host_target_known_and_unknown():
    assert db.host_target("Linux", "x86_64") == LINUX
    assert db.host_target("Darwin", "arm64") == MAC
    assert db.host_target("Linux", "aarch64") is None
    assert db.host_target("Windows", "AMD64") is None


def test_asset_name_carries_one_v():
    assert db.asset_name("2026.11.1", LINUX) == "towpath-v2026.11.1-x86_64-unknown-linux-gnu.tar.gz"
    assert db.asset_name("v2026.11.1", MAC) == "towpath-v2026.11.1-aarch64-apple-darwin.tar.gz"


def test_asset_url_for_both_targets():
    base = "https://github.com/ppfenning/coxswain-dash/releases/download/v2026.11.1/"
    assert db.asset_url("ppfenning/coxswain-dash", "v2026.11.1", LINUX) == (
        base + "towpath-v2026.11.1-x86_64-unknown-linux-gnu.tar.gz"
    )
    assert db.asset_url("ppfenning/coxswain-dash", "v2026.11.1", MAC) == (
        base + "towpath-v2026.11.1-aarch64-apple-darwin.tar.gz"
    )


def test_installed_version_parse():
    assert db.installed_version("towpath 2026.11.1") == "2026.11.1"
    assert db.installed_version("towpath v2026.11.1\n") == "2026.11.1"
    assert db.installed_version("") is None
    assert db.installed_version("   \n") is None
    assert db.installed_version("towpath v") is None
    assert db.installed_version("towpath 2026.11.1+14df4b9") == "2026.11.1+14df4b9"


def test_binary_status():
    assert db.binary_status("v2026.11.1", "2026.11.1") == "ok"
    assert db.binary_status("v2026.11.1", "2026.10.5") == "drift"
    assert db.binary_status("v2026.11.1", None) == "missing"
    assert db.binary_status("v2026.11.1", "2026.11.1+14df4b9") == "ok"
    assert db.binary_status("v2026.11.1", "2026.10.5+14df4b9") == "drift"


def test_dev_build_argv():
    assert db.dev_build_argv("/src/dash") == [
        "cargo",
        "build",
        "--release",
        "--manifest-path",
        "/src/dash/Cargo.toml",
    ]


def test_dev_link_argvs():
    assert db.dev_link_argvs("/src/dash", "/opt/towpath/bin") == [
        ["ln", "-sf", "/src/dash/target/release/towpath", "/opt/towpath/bin/towpath"],
        ["ln", "-sf", "/src/dash/target/release/coxtop", "/opt/towpath/bin/coxtop"],
    ]


def test_binary_step_with_target():
    assert db.binary_step("dash", SPEC, LINUX, "/opt/bin") == {
        "kind": "binary",
        "component": "dash",
        "detail": "install towpath, coxtop v2026.11.1 for x86_64-unknown-linux-gnu into /opt/bin",
        "url": "https://github.com/ppfenning/coxswain-dash/releases/download/v2026.11.1/"
        "towpath-v2026.11.1-x86_64-unknown-linux-gnu.tar.gz",
        "target": LINUX,
        "bin_dir": "/opt/bin",
        "binaries": ["towpath", "coxtop"],
    }


def test_binary_step_without_target_is_skip():
    assert db.binary_step("dash", SPEC, None, "/opt/bin") == {
        "kind": "skip",
        "component": "dash",
        "detail": "no release asset for this machine; build towpath from source with "
        "cargo install --git https://github.com/ppfenning/coxswain-dash --tag v2026.11.1",
    }
