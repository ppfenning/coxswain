# review-diff

review-diff runs the charter reviewer, the adversary, verify-evidence and
arbitration on a diff that is not a run's own build. It takes a diff, a
repository and a ref, and returns the same verdict-and-findings shape a
build's review step produces. Nothing is written to the repository and
nothing is pushed. `verify_evidence` runs only when the cartridge
configures checks. `cox runs review --pr <url>` runs it on a pull
request and posts the verdict as a review.

--8<-- "components/graphs/docs/graphs/review-diff.md"
