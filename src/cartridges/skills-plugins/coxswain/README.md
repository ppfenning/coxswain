# coxswain

The plugin that `cox` loads into every Claude Code session it starts.

- A `SessionStart` hook runs `hooks/docket.sh`, which prints the current
  routing, intake, runs and ready lines from `cox route context` (falling
  back to `agent-tools route context`) once a Coxswain profile exists, and
  says nothing otherwise.
- The plugin's own `settings.json` wires `statusline/statusline.sh` in as
  the statusLine command. It reports the model name plus how many runs are
  in flight against the slot cap and recent spend, read from
  `cox route status --json`, falling back to the bare model name whenever
  `cox`, `python3`, or the Coxswain profile is missing.

If a Claude Code build ignores a plugin's own `statusLine` key, copy the
block from `settings.json` into your own to enable it by hand.

Slash commands to launch and land runs from the prompt arrive later.
