# Multi-Agent Collaboration Patterns

Use native multi-agent tools for orchestration and the blackboard for durable
evidence, work ownership, duplicate suppression, and exclusive-resource control.
Roles are temporary responsibilities, not permanent agent identities.

## Fan-out / fan-in

### Fan-out

Split work only when branches are independently useful and have clear boundaries.
Every assignment should name:

- objective and non-goals;
- role and expected deliverable;
- evidence required;
- relevant intent, activity, or resource key;
- success, disproval, or budget stop condition.

Prefer a few bounded branches over many vague “investigate everything” tasks. Do
not fan out tightly coupled sequential work or give multiple agents the same
direction unless one is explicitly an independent verifier.

```text
Role: explorer
Objective: determine whether <hypothesis> is viable
Scope: <targets/files/endpoints>
Deliverable: facts, scoped dead ends, and one recommended next step
Evidence: commands, requests, output excerpts, or artifact paths
Stop when: <success, disproval, or budget condition>
```

### Fan-in

Integrate outcomes instead of concatenating transcripts:

1. Collect each native handoff.
2. Read its referenced board facts, dead ends, artifacts, and review state.
3. Separate verified evidence from candidates.
4. Send material conflicts to an independent verifier.
5. Let a reviewer check scope, completeness, and unsupported conclusions.
6. Choose the next branch or produce the final result.

Missing evidence is not a negative result. Do not close a branch merely because its
worker timed out or stopped.

## Role templates

| Role | Responsibility | Typical durable output |
|---|---|---|
| **Coordinator** | Decompose work, assign owners, resolve priorities, integrate handoffs | Intents, assignments, integration decision |
| **Explorer** | Search a bounded hypothesis space and eliminate weak routes | Candidate facts, verified observations, scoped dead ends |
| **Implementer** | Build the selected code, artifact, query, exploit, or procedure | Artifact paths, implementation facts, test results |
| **Verifier** | Independently reproduce an important claim | Verified fact, failed reproduction, or challenge evidence |
| **Reviewer** | Check contradictions, completeness, scope, and integration | Review finding, branch recommendation, reopen/suppress guidance |

Use different agents for implementation and verification when an error would affect
a terminal, destructive, expensive, or externally visible conclusion.

## Choose intent, activity, or resource

| Mechanism | Use when | Example |
|---|---|---|
| **Intent** | One worker should own a direction and conclusion | `analyze authentication flow` |
| **Activity** | Work is expensive and duplication wastes time, but parallel execution is safe | `decompile:client.exe`, `build:frontend` |
| **Resource** | Parallel use may conflict, mutate state, consume attempts, or steal exclusive access | `account:admin@service`, `listener:4444@worker-a` |

These mechanisms are complementary. A worker may own an intent, claim an activity
for its expensive phase, and briefly lock a resource for a conflicting action.

- A native assignment does not replace a required board claim.
- Proceed only after `WON`.
- Renew long leases before expiry.
- Release activity and resource leases during cleanup.
- A lost claim is a coordination result, not evidence against the technical route.

## Use native messages and durable writes together

- **Native message:** low-latency assignment, question, progress signal,
  interruption, conflict notice, or handoff.
- **Blackboard:** evidence or control state another agent may need after the current
  message or worker disappears.

For urgent reusable evidence, write the board record first and then send a message
with its sequence/reference and impact. Do not copy chat transcripts or routine
progress chatter into the board.

## Resolve conflicts

Resolve conflicts by evidence and ownership, not voting:

1. Stop using the disputed claim as a premise.
2. Preserve both observations with scope and provenance.
3. Check whether they describe different environments or assumptions.
4. Split incompatible assumptions into separate branches.
5. Assign an independent verifier the smallest decisive test.
6. Revalidate, reject, merge, or supersede from new evidence.
7. Notify workers whose active work depended on the old claim.

For ownership conflicts, the live lease owner continues. A stale worker may publish
useful evidence as a late report, but must not overwrite a newer intent owner or
release the newer lease.

## Handoff template

```text
HANDOFF
Role / objective: <role and bounded task>
Intent: <intent ID or none>
Status: <completed | partial | disproved | blocked>

Durable outputs:
- Fact <seq>: <finding>
- Dead end <seq>: <scope and reopen condition>
- Artifact: <absolute path, commit, query, or command>

Evidence:
- <minimal reproduction command/request>
- <key observed output>

Unresolved:
- <candidate, contradiction, or missing test>

Cleanup:
- Activities released: <keys or none>
- Resources released: <keys or none>

Recommended next step:
- <one concrete action and suggested role>
```

The handoff should let another agent continue without replaying the transcript.

## Common anti-patterns

- Using the board as chat or an execution log.
- Keeping important evidence only in native messages.
- Fanning out vague or overlapping assignments.
- Having an implementer “independently” verify its own conclusion.
- Treating a candidate, directive, or review opinion as verified evidence.
- Marking a route dead after one timeout or transient failure.
- Continuing after a `LOST` claim or failed renewal.
- Reusing a worker identity after restart.
- Holding resource locks while idle.
- Mixing results from incompatible branches.
- Resolving contradictions by majority vote.
- Completing an intent without durable output or a scoped negative result.
- Polling the whole board repeatedly instead of reading relevant/incremental state.
