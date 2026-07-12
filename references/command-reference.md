# Multi-agentcollaboration-blackboard CLI Reference

## Invocation and scope

Invoke the bundled script by absolute path. Global options must appear before the
subcommand:

```text
blackboard.py [--db PATH] [--run-id ID] [--actor WORKER]
              [--intent-id ID] <command> ...
```

Use Python 3.9 or newer.

Prefer coordinator-provided environment variables over repeating global options:

| Variable | Meaning |
|---|---|
| `MULTI_AGENT_COLLABORATION_BLACKBOARD_DB` | Existing SQLite board path |
| `MULTI_AGENT_COLLABORATION_RUN_ID` | Run scope stored in `challenge_id` columns |
| `MULTI_AGENT_COLLABORATION_WORKER_ID` | Unique process identity; never reuse after restart |
| `MULTI_AGENT_COLLABORATION_INTENT_ID` | Current bound intent for provenance |

Legacy `INFINITEX_*` variables remain accepted. CLI options override environment
variables. Canonical and legacy variables must agree when both are set; conflicts
fail closed. `--challenge-id` remains an alias for `--run-id`.

Use one DB per run. A `.multi_agent_collaboration_blackboard` marker may contain
either the SQLite DB itself or one UTF-8 path to it. The legacy
`.infinitex_blackboard` marker remains accepted. Direct fallback names are
`multi_agent_collaboration_blackboard.db` and legacy `shared_graph.db`. Conflicting
markers or multiple direct fallback DBs fail closed.

Run `doctor` to print the selected DB, run/challenge, actor, journal mode, and tables.
Use `doctor --strict` when validating against the latest upstream schema rather
than a compatible older board.
Mutation and ownership commands fail closed when no stable worker ID is configured.

## Read commands

| Command | Purpose |
|---|---|
| `read-directives` | Active operator directives |
| `read-deadends [--limit 200] [--since-seq N]` | Ruled-out directions |
| `read-review` | Findings, challenged facts, routes, and branches |
| `read-facts [--verified-only] [--limit 200] [--since-seq N]` | Active facts using effective review state |
| `read-routes` | Suppressed and reopened routes |
| `read-branches` | Forked assumptions and verification criteria |
| `read-flags` | Valid flags; invalidated flags stay rejected |
| `list-intents` | Open, active, claimable intents |
| `list-activities` | Live expensive-work leases |
| `read-resource-locks` | Live exclusive-resource leases |
| `directive-status ID` | Delivery/binding state of one directive |

Malformed event payloads are skipped or labeled instead of crashing the whole read.
Agent-facing text is flattened to one line and stripped of ANSI/control characters.
Fact/dead-end reads default to the latest 200 entries and support sequence-based
incremental reads. All supported reads are run-scoped.

## Evidence commands

```text
write-fact "<objective finding>" [--verified]
mark-deadend "<scope; test; result; reopen condition>"
```

Fact identity matches the coordinator's normalization: strip one leading engine
tag, fold whitespace, and ignore case. Rewriting an existing candidate with
`--verified` atomically promotes it and retains its intent-product link. A requested
intent-product link is accepted only when the intent is claimed+active, owned by the
same actor, and its lease is still live; otherwise the fact is marked as a late
report and a warning is emitted.

## Intent leases

```text
list-intents
claim INTENT_ID [--lease-seconds 300]
renew-intent INTENT_ID [--lease-seconds 300]
release-intent INTENT_ID
complete-intent INTENT_ID [--result explored] [--detail TEXT]
                [--to-fact-seq N]
```

- `claim` prints `WON` or `LOST`. Start only on `WON`.
- `renew-intent` is owner-fenced, requires a still-live lease, and prints `WON` or
  `LOST`.
- `release-intent` reopens an owned direction and prints `OK` or `LOST`.
- `complete-intent` writes an `intent_concluded` event and closes the owned intent.
  It prints `OK`, `STALE` for a late non-owner conclusion, or `LOST` if absent.
  As in the coordinator, a validated `solved` result and actor `coordinator` bypass
  the owner fence because they terminate the run globally.
- Renew before the lease expires; do not rely on repeated `claim` as a heartbeat.

## Duplicate-work activity leases

Use an activity key shaped like `verb:target`, for example `nmap:10.0.0.8` or
`decompile:client.exe`.

```text
claim-activity KEY [--lease-seconds 600]
renew-activity KEY [--lease-seconds 600]
release-activity KEY
list-activities
```

Activities suppress duplicated expensive work but do not authorize destructive or
exclusive access. Renew only as the current owner before a long activity lease
expires.

## Exclusive resource leases

Use a resource-only key, not a description of the intended action. A useful shape
is `risk:transport:port@host`, `account:user@service`, or `listener:port@worker`.

```text
read-resource-locks
claim-resource KEY [--scope activity] [--risk-class destructive]
                   [--lease-seconds 600]
release-resource KEY
```

Only the current owner can release a live lock. Reclaiming the same key as the same
worker refreshes its lease. Always release it in cleanup.

## Failure behavior

- Exit `0`: command executed; inspect `WON`/`LOST` where applicable.
- Exit `2`: DB discovery, run scope, schema, identity, configuration conflict, or
  SQLite error.
- Exit `130`: interrupted.

After one bounded retry, switch to native-only collaboration for the current turn.
Do not create or migrate a guessed coordinator DB from a worker session.
