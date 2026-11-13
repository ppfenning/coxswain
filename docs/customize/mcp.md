# MCP server

`cox mcp serve` gives an MCP client the chair's board, queue and cartridge
settings as tools. Run it on the machine that runs the chair and holds your
cartridge.

```sh
cox mcp serve
```

With no flags it speaks MCP on stdin and stdout, which is how a local client
launches a server. A client entry looks like this:

```json
{"mcpServers": {"coxswain": {"command": "cox", "args": ["mcp", "serve"]}}}
```

`--label NAME` names the client on every entry it sends. The default is
`mcp`.

## Tools that read

Each answers compactly by default and gives detail only when asked.

| Tool | Returns |
| --- | --- |
| `release_status` | Release facts, and done and open task counts per initiative |
| `queue_board` | Task counts and cost per state; `detail` gives the rows |
| `explain` | Why an initiative did not launch, or a task's state and quarantine reason |
| `recent_events` | The newest typed entries on the courier bus |
| `cartridge_settings` | The cartridge's and profile's settings, read from their own files |

`explain` answers "why did this not launch" from the chair's last tick. The
chair writes those reasons to `runs/chair.unlaunched.json` after each tick
and replaces them on the next.

`cartridge_settings` returns the facts. Putting them into words is the
client's job.

## Tools that write

A plain write runs the `cox` command you would type, as its own process, and
returns its real outcome: `done`, or `refused` with what the command said.
Each one also leaves a `chat_action` entry on the courier bus, as a record.

| Tool | Runs |
| --- | --- |
| `file_intake` | `cox route file --intake` |
| `set_stage` | `cox route ready` or `cox route draft` |
| `set_priority` | `cox route priority --set` |
| `send_directive` with `hold` or `release` | `cox route hold` or `cox route release` |
| `set_setting` | `cox settings set cartridge` |

`set_setting` returns the diff and writes nothing until the call passes
`confirm: true`. The write is committed in the cartridge's own git when the
file is tracked. Operator-only settings are refused: change those with
`cox settings set` yourself.

Three requests are not carried out by the server. They are sent to the chair
as a directive and return `sent`:

- `cut_release`, which also needs `confirm` equal to the version
- `send_directive` with `scope`
- `send_directive` with `enable`

Your cartridge stays yours. The server edits the file where you keep it and
never writes it to the store; the next launch publishes the new files to
the lane hosts.

## Over HTTP

```sh
cox mcp serve --http 127.0.0.1:8765 --tokens-file ~/.config/agent-tools/mcp-tokens
```

- The address must be a literal loopback, private, tailnet or link-local
  address. A name or a public address is refused.
- The tokens file holds one `label token` per line; a token is at least 24
  characters. The label of the token a client presents names it on every
  entry it sends.
- There is no TLS. Use it on a tailnet or through ssh.
