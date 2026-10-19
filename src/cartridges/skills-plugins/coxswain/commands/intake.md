---
description: File an intake item from freeform text.
argument-hint: <text to file as intake>
---

File an intake item.

Take the repository from what is named in the text; if none is named, ask
rather than guess. Write a short title yourself, and put the text in a
temp file to use as the body.

Run `cox route file --intake --repo <repository> --title <title> --body
<temp file>` (fall back to `agent-tools route file --intake` if `cox` is
not on PATH), and report the path it wrote.

Text: $ARGUMENTS
