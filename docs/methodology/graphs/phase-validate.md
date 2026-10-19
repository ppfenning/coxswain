# phase-validate

phase-validate holds the two validators the epic driver asks for at each
phase boundary. `validate_chunk` asks, per task, whether the task met its
own description. `validate_phase` asks once whether what now exists meets
the phase's original goal. A builder's own summary is stripped before
either prompt is built, so a validator judges evidence, not the owner's
account. The graph emits no proposals; the driver decides what the
verdict costs. It runs as `validate`, invoked by the epic driver.

--8<-- "components/graphs/docs/graphs/phase-validate.md"
