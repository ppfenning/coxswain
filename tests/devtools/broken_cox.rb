# DELIBERATELY BROKEN: negative control for install-proof; never copy into the tap.
class Cox < Formula
  desc "Deliberately broken cox formula"
  homepage "https://example.invalid/cox"
  url "https://example.invalid/does-not-exist/cox-0.0.0-negative-control.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  version "0.0.0-negative-control"

  def install
    bin.install "cox"
  end
end
