# Code-style charter — TEMPLATE

Copy to `cartridges/<team>/context/code-style.md` and **write it in your own
words**. The substrate has no style opinion; the reviewer role enforces
whatever this file says.

> **Write this from conviction, not from a prior employer's file.** What you
> believe about how code should be written is yours and travels with you. The
> specific document you wrote for a previous team is theirs and does not. See
> [`docs/CLEAN-ROOM.md`](../docs/CLEAN-ROOM.md). Rewriting is also just better:
> you will sharpen the second time through.

Keep it short enough that a reviewer actually reads it every time. One page.
State the conviction, then the concrete tell that it has been violated — a
principle a reviewer cannot detect is decoration.

---

## Suggested skeleton

Delete what does not apply, add what is missing. Headers below are ordinary
programming concepts, not a required schema.

### 1. <Principle>

One or two sentences on what you believe and why. Then the tell.

*Worked example of the format, not a prescription:*

> **Immutable bindings.** A name is a definition, not a slot. Rebinding a name
> to mean something new mid-function makes the reader hold two meanings at
> once. New value, new name.
>
> *The tell:* a variable assigned twice in one scope; `x = f(x)`; mutation of a
> caller's argument.

### 2. <Principle>
### 3. <Principle>

---

## Test expectations

Say plainly where tests are expected and where they are not, and why. A blanket
"write tests" gets ignored; "unit tests required for the transform layer, not
for per-client glue" gets followed because it is a decision someone made.

## What this charter does NOT cover

Name the things you have deliberately left to taste, so reviewers stop
relitigating them. Formatting handled by a formatter, import order, naming
minutiae — if a tool decides it, the charter should not.
