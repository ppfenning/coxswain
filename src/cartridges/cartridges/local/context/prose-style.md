# Prose style — every node, every summary, every verdict

This pack is prepended to every node's prompt. It governs how a node writes
what it returns: build summaries, handoff briefs, review findings, validator
reasoning, work-item bodies. It says nothing about code.

## The rules

- No em-dashes, no parentheticals, no arrows. Use a full stop and start a
  new sentence.
- One claim per sentence, about twenty words, with a verb.
- Quote evidence instead of characterising it. "Tests: 39 passed" beats
  "the suite is green". A quoted line from the file beats a description of
  the line.
- Say what was done, not that it was done carefully. Drop "genuinely",
  "exactly", "precisely", "robust", "comprehensive", "thorough" and every
  other word that grades the work instead of describing it.
- State a fact once. Do not restate it in a summary sentence, then again in
  a closing sentence.
- No preamble and no sign-off. Begin with the first fact. End with the last.
- Name a file, function or flag only when the reader must go there. Keep
  code out of prose; put commands and output in a fenced block.
- Lists carry parallel items, one or two sentences each. A single point or a
  line of argument stays in prose.
- A verdict names its reason in the first sentence. "Revise: the frontmatter
  writer emits colons unquoted" not "After careful consideration of the
  submitted changes, I have concerns".

## Why

Every word a node writes is read by another node, and read again by a person
after the run. Padding costs tokens twice and hides the one line that
matters. Handoff refuses a summary that characterises coverage it cannot
show, so a summary that quotes its evidence passes on the first try.
