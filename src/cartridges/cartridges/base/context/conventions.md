# Base conventions (context pack)

Principles that hold for any team using this substrate. A team pack layers
specifics on top; it does not restate these.

## Single writer

Exactly one component is the writer of any given board or state machine. If two
things can move a ticket, the board's state is a race, and nobody can reason
about it. Every other participant proposes; the single writer applies.

## Evidence before writes

A proposal that asserts something is true must carry the check that proves it,
and the check must be deterministic — a command and its output, not a
recollection. "The pipeline recovered" is a claim. "Here is the object listing
showing the file landed at 04:12Z" is evidence.

Corollary: absence of an error is not evidence of success. A job that reports
SUCCESS while writing nothing has not succeeded, and a graph that trusts the
status field will confidently tell you so.

## Propose, don't write

Default posture for anything touching a system of record is propose-only. The
agent investigates, drafts a recommendation, and stops. A human decides. This
is not a training-wheels phase to be outgrown quickly — it is the design. Kinds
graduate individually, on evidence, and a single reversal sends one back.

The reason is asymmetry: a wrong proposal costs a minute of review, and a wrong
write costs an incident. Autonomy is worth buying only where that ratio has
been measured, per kind, not assumed globally.

## Attribution

Agent-generated content that a human will read is marked as such — except in
surfaces where the human is accountable for the content regardless. A code
review comment is the reviewer's own voice and carries their judgment. A
tracker comment summarizing an automated run is marked agentic, because the
reader needs to know how much to trust it.

## Incidents lead with the conclusion

Bottom line up front: what broke, what the impact was, what the state is now.
Chronology after. Someone reading at 3am needs the answer in the first
sentence, not the investigation that produced it.

## Breadcrumbs

Anything discovered the hard way gets written down where the next person will
trip over the same thing — beside the code, not in a document nobody opens. The
test of a good breadcrumb is whether it names the wrong belief, not just the
right answer: "this returns local time, not UTC" beats "use head-object here".
