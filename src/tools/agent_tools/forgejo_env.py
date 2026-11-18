"""The environment variable names the forgejo forge reads, in a module that imports nothing.

`unit_env` needs these names on the `cox route chair status` path; importing them from `forge_forgejo` pulled in
`forge_github`, `land` and `chair_types` with them (about 14 ms of startup). `forge_forgejo` re-exports both."""

from __future__ import annotations

DEFAULT_TOKEN_ENV = "FORGEJO_TOKEN"
# Where an ssh origin finds its API address: the origin names a host, not an http(s) server.
BASE_URL_ENV = "FORGEJO_BASE_URL"
