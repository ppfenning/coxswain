import pytest

from devtools import release

OLD_URL = "https://x/cox-tools-0.1.0.tar.gz"
NEW_URL = "https://x/cox-tools-0.2.0.tar.gz"
OLD_SHA, NEW_SHA = "0" * 64, "1" * 64
MAC_URL, MAC_SHA = "https://x/towpath-0.2.0-aarch64-apple-darwin.tar.gz", "m" * 64
LIN_URL, LIN_SHA = "https://x/towpath-0.2.0-x86_64-unknown-linux-gnu.tar.gz", "l" * 64
ASSETS = {
    release.TOWPATH_MACOS_ARM: (MAC_URL, MAC_SHA),
    release.TOWPATH_LINUX_INTEL: (LIN_URL, LIN_SHA),
}
STAGE = ('    if resources.map(&:name).include?("towpath")\n      resource("towpath").stage do\n'
         '        bin.install "towpath", "coxtop"\n      end\n    end\n')
WITHOUT = ' without: resources.map(&:name) & ["towpath"]'
TEST_LINE = '    system bin/"towpath", "--version" if (bin/"towpath").exist?\n'

HEAD = f'''class CoxTools < Formula
  include Language::Python::Virtualenv

  url "{OLD_URL}"
  sha256 "{OLD_SHA}"

  resource "pyyaml" do
    url "https://x/pyyaml-6.0.tar.gz"
    sha256 "{"a" * 64}"
  end

  resource "zstandard" do
    url "https://x/zstandard-0.22.tar.gz"
    sha256 "{"b" * 64}"
  end
'''

TAIL = '''
  def install
    virtualenv_install_with_resources
  end

  test do
    system bin/"cox", "--version"
  end
end
'''

PLAIN = HEAD + TAIL


def _towpath(url: str, sha: str, scope: str, arch: str) -> str:
    return (f'  {scope} do\n    {arch} do\n      resource "towpath" do\n        url "{url}"\n'
            f'        sha256 "{sha}"\n      end\n    end\n  end\n')


OLD_MAC = ("https://x/towpath-0.1.0-aarch64-apple-darwin.tar.gz", "c" * 64)
OLD_LIN = ("https://x/towpath-0.1.0-x86_64-unknown-linux-gnu.tar.gz", "d" * 64)
WITH_BLOCK = (HEAD + "\n" + _towpath(*OLD_MAC, "on_macos", "on_arm") + "\n" + _towpath(*OLD_LIN, "on_linux", "on_intel")
              + "\n  def install\n" + STAGE + "    virtualenv_install_with_resources" + WITHOUT + "\n  end\n\n  test do\n"
              + '    system bin/"cox", "--version"\n' + TEST_LINE + "  end\nend\n")


def _bump(text: str, **kw) -> str:
    return release.bumped_formula_text(text, "0.2.0", NEW_URL, NEW_SHA, **kw)


def _bumped(text: str) -> str:
    return text.replace(OLD_URL, NEW_URL).replace(OLD_SHA, NEW_SHA)


def test_adds_towpath_block_install_line_and_test_line_to_a_formula_without_one():
    expected = _bumped(
        HEAD + "\n" + _towpath(MAC_URL, MAC_SHA, "on_macos", "on_arm") + "\n" + _towpath(LIN_URL, LIN_SHA, "on_linux", "on_intel")
        + "\n  def install\n" + STAGE + "    virtualenv_install_with_resources" + WITHOUT + "\n  end\n\n  test do\n"
        + TEST_LINE + '    system bin/"cox", "--version"\n  end\nend\n')
    out = _bump(PLAIN, towpath=ASSETS)
    assert out == expected
    assert out.count('resource "towpath"') == 2
    assert out.count('resource("towpath").stage') == 1
    assert out.count('"towpath", "--version"') == 1


def test_block_goes_before_def_install_when_the_formula_has_no_resources():
    source = ('class C < Formula\n  url "u"\n  sha256 "s"\n\n  def install\n    virtualenv_install_with_resources\n  end\n\n'
              '  test do\n    system bin/"c", "--version"\n  end\nend\n')
    out = _bump(source, towpath=ASSETS)
    assert out.count('resource "towpath"') == 2
    assert out.index('resource "towpath"') < out.index("def install") < out.index('resource("towpath").stage')


def test_an_existing_equivalent_test_line_or_stage_line_is_not_duplicated():
    tested = PLAIN.replace('"--version"\n', '"--version"\n    assert_match version.to_s, shell_output("#{bin}/towpath --version")\n')
    staged = PLAIN.replace("  def install\n", "  def install\n" + STAGE)
    assert TEST_LINE not in _bump(tested, towpath=ASSETS)
    assert _bump(staged, towpath=ASSETS).count('resource("towpath").stage') == 1


def test_rewrites_an_existing_towpath_block_without_duplicating_anything():
    out = _bump(WITH_BLOCK, towpath=ASSETS)
    for old in (*OLD_MAC, *OLD_LIN):
        assert old not in out
    for new in (MAC_URL, MAC_SHA, LIN_URL, LIN_SHA):
        assert out.count(new) == 1
    assert out.count('resource "towpath"') == 2
    assert out.count('resource("towpath").stage') == 1
    assert out.count('"towpath", "--version"') == 1
    assert out == _bumped(WITH_BLOCK).replace(OLD_MAC[0], MAC_URL).replace(OLD_MAC[1], MAC_SHA).replace(
        OLD_LIN[0], LIN_URL).replace(OLD_LIN[1], LIN_SHA)


def test_a_second_run_over_its_own_output_changes_nothing():
    once = _bump(PLAIN, towpath=ASSETS)
    assert _bump(once, towpath=ASSETS) == once


@pytest.mark.parametrize("source", [PLAIN, WITH_BLOCK])
def test_a_mapping_missing_a_platform_raises_on_both_paths(source):
    with pytest.raises(KeyError):
        _bump(source, towpath={release.TOWPATH_MACOS_ARM: (MAC_URL, MAC_SHA)})


def test_a_platform_with_no_block_to_rewrite_raises_rather_than_stay_stale():
    mac_only = HEAD + "\n" + _towpath(*OLD_MAC, "on_macos", "on_arm") + TAIL
    with pytest.raises(ValueError, match=release.TOWPATH_LINUX_INTEL):
        _bump(mac_only, towpath=ASSETS)


def test_a_towpath_block_under_unrecognised_scopes_raises():
    odd = HEAD + '  if Hardware::CPU.arm?\n    resource "towpath" do\n      url "u"\n      sha256 "s"\n    end\n  end\n' + TAIL
    with pytest.raises(ValueError, match="unrecognised scopes"):
        _bump(odd, towpath=ASSETS)


def test_a_formula_with_no_anchor_raises_instead_of_returning_it_unchanged():
    with pytest.raises(ValueError, match="anchor"):
        _bump("not a formula\n", towpath=ASSETS)


@pytest.mark.parametrize("missing, match", [
    ("  def install\n    virtualenv_install_with_resources\n  end\n", "def install"),
    ("virtualenv_install_with_resources", "virtualenv_install_with_resources"),
    ('  test do\n    system bin/"cox", "--version"\n  end\n', "test do"),
])
def test_the_add_path_raises_when_the_install_pip_or_test_line_has_no_anchor(missing, match):
    # a formula that gets the resource blocks but not the matching line would look bumped and ship without towpath
    source = PLAIN.replace(missing, "    bin.install \"cox\"\n" if missing.startswith("virtualenv") else "")
    assert source != PLAIN
    with pytest.raises(ValueError, match=match):
        _bump(source, towpath=ASSETS)


def test_a_pip_call_with_arguments_raises_rather_than_leave_towpath_on_pip():
    source = PLAIN.replace("virtualenv_install_with_resources", "virtualenv_install_with_resources(system_site_packages: false)")
    with pytest.raises(ValueError, match="virtualenv_install_with_resources"):
        _bump(source, towpath=ASSETS)


def test_an_existing_block_with_a_trailing_comment_is_rewritten_not_duplicated():
    commented = WITH_BLOCK.replace('resource "towpath" do\n', 'resource "towpath" do # pinned\n')
    out = _bump(commented, towpath=ASSETS)
    assert out.count('resource "towpath"') == 2
    assert MAC_URL in out and LIN_URL in out
    assert not any(old in out for old in (*OLD_MAC, *OLD_LIN))


def test_sdist_lines_and_the_pyyaml_and_zstandard_resources_are_identical_before_and_after():
    resources = HEAD[HEAD.index('  resource "pyyaml"'):]
    for source in (PLAIN, WITH_BLOCK):
        out = _bump(source, towpath=ASSETS)
        assert f'  url "{NEW_URL}"\n  sha256 "{NEW_SHA}"\n' in out
        assert resources in out


def test_none_returns_the_old_output():
    assert _bump(PLAIN) == _bumped(PLAIN)
    assert _bump(WITH_BLOCK, towpath=None) == _bumped(WITH_BLOCK)
