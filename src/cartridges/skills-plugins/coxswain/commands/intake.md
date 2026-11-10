---
description: File an intake item from freeform text.
argument-hint: <text to file as intake>
---

File an intake item.

Take the repository from what is named in the text; if none is named, ask
rather than guess. Write a short title yourself, and put the text in a
temp file to use as the body.

If `file_intake` appears in this session's tool list, call it with the
repository, the title, the text as the body, and stage `draft`. If the tool
is absent, or the call fails because the server cannot be reached, run `cox
route file --intake --repo <repository> --title <title> --body <temp file>`
instead (fall back to `agent-tools route file --intake` if `cox` is not on
PATH). If the chair refuses the entry, report the refusal and stop; do not
retry it through the other route.

Report the path or id it wrote, and say which route was used.

Text: $ARGUMENTS
