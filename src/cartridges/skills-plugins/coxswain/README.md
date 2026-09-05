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

## Commands

- `/launch` — route a request through route-work; file and launch it if it
  changes a repository, else answer inline.
- `/land` — dry run `cox runs land`, show the plan, then apply only once
  The operator says to, and report the PR URL and merge state.
- `/runs` — show `cox route status` and the latest `cox runs events`.
- `/intake` — file freeform text as an intake item with `cox route file
  --intake`.
- `/regatta` — open the regatta window if one is installed, else show run
  status.
