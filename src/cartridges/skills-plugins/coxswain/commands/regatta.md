---
description: Open the regatta window if one is installed, else show run status.
argument-hint: (no arguments)
---

Open the regatta window.

If `runs-top-float` or a regatta launcher is on PATH, run it to open the
window.

Otherwise, say plainly that the regatta window is planned but not
installed here, and show `cox route status` (fall back to `agent-tools
route status`) instead so there is still something to look at.
