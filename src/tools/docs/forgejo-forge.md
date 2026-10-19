# The forgejo forge

This page is for someone adding a repository hosted on Forgejo to the profile. It covers what the forge does, how it finds the server and the token, the token the Forgejo side must issue, and how to keep a Forgejo repository next to a GitHub one.

## What the forgejo forge does

The forgejo forge lands a task through a pull request on a Forgejo server, using the Forgejo REST API. It plays the same part that `github` plays for a repository on github.com.

It opens a pull request from the task's branch. Before it opens one, it looks for an open pull request on the same branch and reuses that number.

It reads the checks from the commit statuses on the pull request's head commit. A status is a check, so any CI that reports commit statuses to Forgejo gates the land. The forge waits for those statuses to settle and stops with the failing context names when one fails.

A head commit with no statuses at all is not a pass. The forge reports no checks and keeps waiting until the land's timeout. A repository with no CI that reports statuses to Forgejo therefore cannot land through this forge.

It asks Forgejo whether the pull request is mergeable. A pull request that Forgejo reports as not mergeable is not merged.

It lands the pull request with a squash merge. The default branch gains one commit for the task.

After the merge it asks Forgejo to delete the task's branch. A failed delete on the server does not fail the land. The result then carries a `branch not deleted` note and the land still counts as merged. The forge also deletes the task's local branch and updates the local default branch. When the task's branch is checked out, it switches the checkout to the default branch first.

## How it is configured

Set `forge: forgejo` in the profile, or `forge: auto` (see below). Nothing
else in the profile configures this forge.

- **The server** comes from the repository's `origin`. An `http://` or
  `https://` origin names the server itself. An ssh origin, such as
  `git@git.example.internal:team/app.git`, names only a host, so the API
  address comes from the `FORGEJO_BASE_URL` variable, which must be on the
  same host. With an ssh origin and no `FORGEJO_BASE_URL`, the land stops
  and names the variable.
- **The token** comes from the `FORGEJO_TOKEN` variable.

```bash
export FORGEJO_BASE_URL=https://git.example.internal
export FORGEJO_TOKEN=<token>
```

The profile keys `forgejo_base_url` and `forgejo_token_env` are not read.
The profile parser ignores them, as it ignores any unknown top-level key,
with a `profile: ignoring unknown key ...` warning, and `cox setup doctor`
shows a warning row for each.

## The token

Create the token in the Forgejo user settings, under applications. Forgejo groups its token scopes by area, and pull requests fall under the repository area. Select these:

- repository read and write, which is `read:repository` and `write:repository` in Forgejo's scope list, to read the repository, read commit statuses and delete the branch
- pull request read and write, which the repository scopes above already grant, to list, open and merge pull requests

The token value is read from `FORGEJO_TOKEN` in the environment. It is not stored in the profile. It is sent only in the `Authorization` header of requests to the Forgejo server.

The token is never logged. Every error message that could carry it passes through a redaction step that replaces it with `***`, including the escaped forms that an HTTP library may quote. When the variable is missing or empty, the error names the variable and says nothing about a value.

## Choosing a forge per repository

`forge` is one value for the whole profile, but `forge: auto` picks per
repository from its `origin`:

- an origin on the host of `FORGEJO_BASE_URL` lands through `forgejo`;
- an origin on github.com lands through `github`;
- any other repository, including one with no remote, lands through `local`.

So one profile can hold a GitHub repository and a Forgejo repository. Set
`FORGEJO_BASE_URL` and `FORGEJO_TOKEN` in every shell that lands, and log
`gh` in for the GitHub ones.
