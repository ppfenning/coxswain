# lifecycle-propose

The coxswain dispatches lifecycle-propose whenever a ticket needs to move
state: open to in-progress, in-progress to review, review to done. It
proposes the transition and the evidence that justifies it; the board's
single writer applies it. What lands is a proposed state change, never a
direct write to the tracker.

--8<-- "components/graphs/docs/graphs/lifecycle-propose.md"

--8<-- "components/graphs/graphs/delivery/lifecycle-propose.md"
