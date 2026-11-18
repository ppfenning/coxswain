"""The profile's `window_ceiling_usd` reaches the usage assessment (cost-bounds.md, section 6, rule 1).

`cox usage assess` reads the routing profile and judges the five-hour window's spend against its ceiling. The window
here is one active block that spent $50, seeded as ccusage's cached answer in the runs folder, the file `gather`
reads before it would launch ccusage. With `window_ceiling_usd: 250` in the profile's `spend:` block the spend is a fifth of the ceiling; without a
ceiling there is no fraction to report. This runs the real installed `cox` on the sandbox's own profile and store.
"""

import datetime
import json

PROFILE = ".config/agent-tools/profile.yaml"
CCUSAGE_CACHE = ".ccusage-block.json"  # usage_window._CCUSAGE_CACHE_FILE


def _assess(sandbox, extra_profile_lines=""):
    now = datetime.datetime.now(datetime.UTC)
    block = {
        "isActive": True,
        "startTime": (now - datetime.timedelta(hours=1)).isoformat(),
        "endTime": (now + datetime.timedelta(hours=4)).isoformat(),
        "costUSD": 50.0,
    }
    runs = sandbox.workspace / "runs"
    (runs / CCUSAGE_CACHE).write_text(json.dumps({"at": now.isoformat(), "blocks": {"blocks": [block]}}), encoding="utf-8")
    profile = sandbox.home / PROFILE
    profile.write_text(profile.read_text(encoding="utf-8") + extra_profile_lines, encoding="utf-8")
    done = sandbox.cox("usage", "assess", "--json", "--runs-dir", str(runs), timeout=120)
    assert "Traceback" not in done.stderr and "unknown key" not in done.stderr, done.stderr
    assert done.stdout.startswith("{"), (done.returncode, done.stdout[-800:], done.stderr[-800:])
    return json.loads(done.stdout)


def test_the_profiles_window_ceiling_is_what_the_spend_is_judged_against(sandbox):
    assessed = _assess(sandbox, "spend:\n  window_ceiling_usd: 250\n")
    assert assessed["spent_fraction"] == 0.2, assessed
    assert assessed["headroom_usd"] is not None, assessed


def test_a_profile_without_a_window_ceiling_judges_no_fraction(sandbox):
    assessed = _assess(sandbox)
    assert assessed["spent_fraction"] is None, assessed
    assert assessed["headroom_usd"] is None, assessed
