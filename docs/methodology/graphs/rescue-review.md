# rescue-review

rescue-review is the review half of the development loop, started from a
patch that already exists. It takes a ticket, the patch and the evidence
rows the harness produced by running the checks, and runs one review
round: handoff, charter reviewer, adversary, and arbitration when the
round calls for it. There is no plan, no build and no fix loop, and the verdict is
final. A shipped verdict proposes a draft pull request. `cox route launch
rescue` starts it for one quarantined task, through the `rescue` driver.

--8<-- "components/graphs/docs/graphs/rescue-review.md"
