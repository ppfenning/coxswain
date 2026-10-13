# DELIBERATELY BROKEN: negative control for install-proof; never copy into the published formula.
# It is the released formula with one change, version "999.0.0": the real previous-release sdist
# installs and upgrades cleanly, so the proof can only reject it at check-version.
class Cox < Formula
  include Language::Python::Virtualenv

  desc "Operator CLI for Coxswain: run records, traces, landing, and the live screens"
  homepage "https://ppfenning.github.io/coxswain/latest/"
  url "https://files.pythonhosted.org/packages/26/fb/5b5d83b8100fd56158d71e7af0d40bf2a34fc8ec774f5c41c2f9916bc94b/coxswain_tools-0.38.0.tar.gz"
  sha256 "c602486d51566fe83588ea97028a0c32be5ecfb5f8b47b6c007a64d2c88bdc49"
  version "999.0.0"
  license "MIT"
  head "https://github.com/ppfenning/coxswain-tools.git", branch: "main"

  depends_on "python@3.14"

  resource "pyyaml" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  resource "zstandard" do
    url "https://files.pythonhosted.org/packages/fd/aa/3e0508d5a5dd96529cdc5a97011299056e14c6505b678fd58938792794b1/zstandard-0.25.0.tar.gz"
    sha256 "7713e1179d162cf5c7906da876ec2ccb9c3a9dcbdffef0cc7f70c3667a205f0b"
  end

  on_macos do
    on_arm do
      resource "towpath" do
        url "https://github.com/ppfenning/coxswain-dash/releases/download/v0.38.0/towpath-v0.38.0-aarch64-apple-darwin.tar.gz"
        sha256 "324d2328a6d1b0cd219a8688febf0e2fead4556a893bab488a76a180c58c82f9"
      end
    end
  end

  on_linux do
    on_intel do
      resource "towpath" do
        url "https://github.com/ppfenning/coxswain-dash/releases/download/v0.38.0/towpath-v0.38.0-x86_64-unknown-linux-gnu.tar.gz"
        sha256 "de92129e7d5c03673876aef8c28a1e7170d1ac13bb98f7838c25a275ac87f830"
      end
    end
  end

  def install
    if resources.map(&:name).include?("towpath")
      resource("towpath").stage do
        bin.install "towpath", "coxtop"
      end
    end
    virtualenv_install_with_resources without: resources.map(&:name) & ["towpath"]
  end

  def caveats
    <<~EOS
      `cox` alone is the CLI. The rest of a Coxswain install — cartridges, graphs,
      crew, the HUD — are git checkouts this command fetches:

        cox setup doctor    # what this machine is missing
        cox install         # every component from the manifest, at the pinned tags

    EOS
  end

  test do
    system bin/"towpath", "--version" if (bin/"towpath").exist?
    assert_match "cox", shell_output("#{bin}/cox --help")
    system bin/"cox", "setup", "doctor"
  end
end
