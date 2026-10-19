# sweep

sweep applies one rule everywhere it matches. `sweep_plan` reads the
repository and writes the map, the exceptions and the application.
`sweep_apply` runs the script, or one build call when the rule cannot be
scripted. `sweep_verify` checks what the apply step captured, with no
model. The review pair on the one resulting diff is review-diff's.
`cox route launch sweep` starts it against an idea, and the coxswain can
dispatch it when the intake queue holds items.

--8<-- "components/graphs/docs/graphs/sweep.md"
