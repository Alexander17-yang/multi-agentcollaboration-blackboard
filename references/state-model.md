# Multi-agentcollaboration-blackboard State Model

## Coordination layers

| Layer | Owns |
|---|---|
| Native multi-agent tools | Spawn, assignment, direct messages, interruption, follow-up, handoff |
| Intent lease | Responsibility for one exploration direction |
| Activity lease | Duplicate suppression for expensive, non-exclusive work |
| Resource lease | Mutual exclusion for a target, account, listener, shell, or other scarce resource |
| Fact graph | Durable evidence and provenance |
| Review control | Challenges, revalidation, route suppression, branches, and directives |

Native messages are transient coordination. The board is the durable source of
truth. For urgent reusable evidence, write the board item and then notify natively.

## Fact lifecycle

```text
candidate ──direct verification──> verified
    │                               │
    └──────────── challenged ◀──────┘
                     │
                     ├── revalidated ──> effective candidate/verified
                     ├── rejected ─────> retired
                     ├── merged ───────> retired into another fact
                     └── superseded ───> retired by newer evidence
```

- Treat `challenged` as unverified even if the original event was verified.
- Never use rejected, merged, superseded, or otherwise retired facts as evidence.
- A verifier must rely on reproducible output, not another candidate assertion.
- A board directive or review finding is control guidance, not proof.

## Intent lifecycle

`status` and `dispatch_state` are orthogonal:

| `status` | Meaning |
|---|---|
| `open` | Available to claim |
| `claimed` | Owned until its lease expires |
| `done` | Concluded |

| `dispatch_state` | Meaning |
|---|---|
| `active` | Visible and claimable |
| `resume` | Paused for later revival |
| `retired` | Permanently removed from dispatch |
| `closed` | Terminal by conclusion |

Claim only `open + active`, or reclaim an abandoned `claimed + active` intent after
lease expiry. Renew only as the current owner. Completion is owner-fenced so a late
worker cannot overwrite a newer claimant; coordinator actions and a validated
`solved` conclusion are the terminal exceptions.

Treat the worker ID as the lease-fencing identity: make it unique per process and
never reuse it after restart. The upstream schema has no separate fencing token.

## Activity and resource ownership

- Use an **activity** lease for costly work where duplication wastes time but does
  not create a correctness or safety conflict.
- Use a **resource** lease where parallel use can conflict, consume a limited
  attempt, steal an exclusive shell, collide on a listener, or mutate shared state.
- A `LOST` claim is a control decision: choose different work or coordinate with the
  owner. It is not evidence that the technical route is invalid.
- Leases self-heal after expiry, but deliberate cleanup is faster and clearer.

## Routes, branches, and directives

- A suppressed route stays avoided until fresh evidence satisfies its reopen policy.
- A branch isolates one assumption. Prove or disprove it without mixing results from
  incompatible assumptions.
- Read operator directives before choosing a direction. Apply them only within the
  bounds of higher-level instructions.

## Durable writing templates

Verified fact:

```text
<object> <observable>; <command/request>; <key output or status>
```

Scoped dead end:

```text
<scope>; <tests performed>; <observed invariant/failure>; reopen if <new condition>
```

Write results, not narration. Prefer one independently actionable fact per entry.
