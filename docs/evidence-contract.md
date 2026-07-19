# The evidence contract

Every operation shell-cli runs produces a structured **evidence record**. This
document is the contract for that record: what it contains, what is redacted from
it, where it is stored, how long it survives, and — the part most worth reading —
what it does *not* guarantee.

Implementation: `shell/evidence.py`. Tests: `tests/test_evidence.py`.

> **Status.** Real and produced. The record shape, redaction scope, versioning,
> storage and degraded-write behaviour are enforced by tests, and `process.exec` /
> `process.shell` now populate these fields from live processes through
> `HostRunner` — `tests/test_process_shell.py` exercises the path with real
> commands. What is still absent is the container runner, so every
> `environment.isolation` you will see today reads `none`.

## Why evidence is a product surface

Four readers consume the same record, and none of them can be served by a log
line:

- the **model**, deciding what to do next from the outcome of the last step;
- the **operator**, auditing what an agent did to their machine;
- **telemetry**, aggregating across runs;
- an **external validator**, which trusts none of the above and needs to check
  the record against what it claims.

That last reader is why the record is versioned, digested, and explicit about its
own gaps. A record an auditor cannot distrust in specific, named ways is not
evidence — it is a summary.

## The record

One JSON object per operation. Top-level blocks:

| Block | Contents |
|---|---|
| *(root)* | `schema_version`, `record_id`, `recorded_at`, `operation_id`, `status` |
| `caller` | `agent`, `task_id`, `tool`, plus `all` — the caller's full provenance map, unmodified |
| `operation` | `kind`, `requested`, `normalized`, `normalized_available` |
| `execution` | `applied`, `handler_entered`, `handler_disposition`, `previewed`, `requested_apply`, `exit_code`, `error`, `started_at`, `ended_at`, `duration_ms`, `resources` |
| `policy` | `decision`, `reason`, `matched_rule` |
| `environment` | `id`, `workspace_kind`, `runner`, `isolation`, `isolation_note`, `root`, `cwd`, `mounts`, `network`, `network_enforced` |
| `output` | `stdout`, `stderr` (each a capture block), `rendering`, `structured` |
| `effects` | `changed_paths`, `bytes_written`, `git_refs`, `created_resources`, `complete` |
| `redaction` | `secret_names`, `replacements`, `placeholder`, `complete`, `scope` |
| `evidence_quality` | `degraded`, `degraded_reason` |
| `persistence` | `persisted`, `path`, `reason`, `write_attempted`, `store_outside_work_root`, `note` |
| `integrity` | `algorithm`, `content_sha256` |

Every block above is present in the **persisted bytes**, not only in the
in-memory record. `tests/test_evidence.py::test_a_record_read_back_off_disk_has_every_documented_block`
writes a record, reads it back off disk, and compares its key set to this table.
That round-trip exists because its absence hid a real divergence: every other
assertion inspected the in-memory record, and a persisted body missing an entire
documented section went unnoticed until a CLI verb tried to read one.

### Both operations are recorded

`operation.requested` and `operation.normalized` are stored separately. They
differ whenever normalization resolved an intent or an execution profile, and an
auditor reconstructing a decision needs both: a record holding only the normalized
form cannot answer "what did the caller actually ask for?".

When an operation failed before normalization — an unknown kind, for instance —
`normalized` is `null` and `normalized_available` is `false`. The requested form
is never copied into both slots to make the record look complete.

### Preview and applied are separate facts

A preview is not a flavour of success anywhere in this package, and a reader must
never have to infer "did this happen?" from parsing a status string.

### `applied` is three-valued, and that is deliberate

`execution.applied` is the field an auditor leans on hardest, so it is the field
most worth stating precisely.

| Value | Meaning |
|---|---|
| `true` | The handler ran to completion and the caller had requested apply. |
| `false` | Nothing was applied, definitively. The handler was never entered. |
| `null` | The handler **was** entered and then crashed. Nobody can say. |

The `null` case is the whole reason this is not a boolean. A status of `failed`
covers two situations with opposite answers:

- **Rejected before the handler was entered** — an unknown kind, a rewrite the
  dispatcher refused, a rewrite that raised, a policy denial, a preview. Dispatch
  returned above the handler call. `applied` is `false`.
- **The handler was entered and raised** — it may have written half a file,
  started a process, or done nothing at all. `applied` is `null`.

Reporting `false` for the second case would be a fabricated all-clear, and
reporting `true` would be a fabricated change. Neither is available, so the
record declines to answer. This is the same posture as `effects.complete` and
`environment.network_enforced`: a thing the code cannot know is reported as
unknown rather than filled in with the convenient value.

Two companion fields make the derivation auditable rather than something to
reverse-engineer from `applied`:

- **`handler_entered`** — `true`, `false`, or `null`. The underlying fact.
- **`handler_disposition`** — `not_reached`, `completed`, `crashed`, or
  `unstated`.

`unstated` appears only on records built outside the dispatch pipeline, by
calling `build_record` directly. Such a caller cannot know how far a pipeline
got, so `applied` degrades to `null` for any non-success status rather than
guessing in the dangerous direction. Records produced by `shell.operations.execute`
never carry `unstated` — dispatch always knows, and always says.

`requested_apply` records what the caller *asked for* and is never conflated with
what happened. An operation denied with `apply=true` reports
`requested_apply: true` alongside `applied: false`; the intention and the event
are separate facts and are stored separately.

### stdout and stderr stay separate

Each stream gets its own capture block:

```json
{
  "text": "...",
  "truncated": false,
  "original_bytes": 16,
  "stored_bytes": 16,
  "sha256": "...",
  "sha256_scope": "text-as-stored"
}
```

`original_bytes` is the size of the stream as produced, when the producer measured
it; `null` means nobody measured it, which is a different fact from zero.

**This diverges from colleague's rendering, deliberately and from day one.**
colleague's `run_command` concatenates stdout and stderr into one unlabelled
string. Preserving that rendering is a compatibility requirement, and capturing
the streams separately is an evidence requirement. Both are satisfied by capturing
separately here and letting colleague's adapter concatenate on its side — a record
that has already merged the streams cannot unmerge them, so the neutral side keeps
the distinction and the compat rendering is derived from it.

### Digests describe what is stored

`output.*.sha256` is taken **after redaction and after truncation**, and
`sha256_scope` says so in the payload. It is not a digest of the original stream.

That is a deliberate refusal. A digest of the unredacted output would be an
offline brute-force oracle for any short secret the record had just removed —
redaction that publishes a checksum of the pre-redaction text has not redacted
anything. So the record attests to its own contents, and the scope is named in
the field rather than left for a reader to assume.

`integrity.content_sha256` covers the canonical serialization of the rest of the
body. A validator recomputes it with the recipe in `integrity.algorithm` and
detects post-hoc tampering. It attests to the *record*; it says nothing about
whether the record described the world accurately.

## Redaction: what is removed, and what is not

This is the section to read before trusting a record.

### Two representations, both scrubbed

An operation produces two things a secret can survive in, and they are redacted
independently:

| Representation | What it is | Redacted by |
|---|---|---|
| the **evidence record** | the durable JSON body, in memory and on disk | `build_record`, always |
| the **`OperationResult`** | the live object returned from `execute` | `execute`, by default |

**Both are scrubbed by default, and that is not a redundancy.** Redacting only
the record protects an auditor reading the trail after the fact, while leaving
the value the *model* reads intact — colleague's `run_command` adapter renders
result output back to the model, so the unscrubbed result was the one path a
declared secret was guaranteed to travel. Protecting the record alone protects
the reader who was never at risk.

### Redacted

**Declared secret values, everywhere they appear.** A caller passes
`secrets={"API_TOKEN": "..."}` to `execute()` (or `capture()` directly); every
occurrence of that value is replaced with `[redacted]` throughout the entire
record body and throughout the returned result — captured stdout and stderr,
operation arguments, structured output, renderings, error messages, policy
reasons, effect lists, and dictionary *keys*. There is no field list a declared
secret can hide behind; `tests/test_evidence.py` and
`tests/test_redaction_boundary.py` pin each side.

Secret **names** are recorded. Secret **values** never are, in any field, at any
time. This matches `Environment.secret_names`, which likewise holds names only.

**No handler is ever given a secret value.** Redaction runs after the handler has
returned, so declaring a secret is not a way to hand one to project code. The
handler signature takes an operation and an environment, and there is nowhere for
a value to arrive — pinned by `test_secrets_never_reach_a_handler`.

Byte counts and digests are **not** recomputed after scrubbing. `stdout_bytes`
describes the stream as the process produced it, which is a fact about the world
rather than about this copy of it; a length shifted to match the placeholder
would misreport what actually ran.

### The one opt-out, and what it does not reach

Some output legitimately *is* the secret — a command that mints a token has to be
able to hand it back. `execute(..., reveal_secrets_in_result=True)` leaves
declared secrets in the returned result.

Three properties make that safe to offer:

- **It is off by default.** A caller who forgets it gets redaction, never
  exposure. `test_the_opt_in_defaults_to_off` asserts the default itself, so
  changing it means walking past an assertion about the change.
- **It never reaches the record.** The persisted evidence is redacted either way.
  A caller can see a minted token live; the durable audit trail still must not
  hold it. This asymmetry is the whole reason the flag can exist.
- **It is recorded as a choice.** `evidence.secret_handling` reports `revealed`,
  so an exposure is never indistinguishable from an ordinary run.

`evidence.secret_handling` is three-valued for the same reason `applied` is:

| Value | Meaning |
|---|---|
| `none_declared` | No secrets were passed. Nothing was checked. |
| `redacted` | Declared values were removed from this result. |
| `revealed` | Declared values were left in place because the caller asked. |

Collapsing `none_declared` and `revealed` into one "not redacted" boolean would
make a deliberate opt-in indistinguishable from a run that never had secrets.

### Not redacted

**Anything that was not declared.** A command that prints a credential nobody
declared writes that credential into the record **and into the result**,
verbatim. Scrubbing the live result closed a gap in *coverage*; it did not widen
the *guarantee*. Only declared values are removed, on both sides.

No pattern heuristics are applied — no "looks like an AWS key", no entropy
threshold. That is a considered choice, not an omission. A scanner catching *some*
undeclared secrets would invite callers to stop declaring them and trust the
scanner instead, converting a visible gap into an invisible one. The gap is worse
when nobody can see it.

`tests/test_evidence.py::test_undeclared_secret_is_NOT_redacted` and
`tests/test_redaction_boundary.py::test_an_undeclared_secret_is_still_present_in_both_representations`
assert the leak directly, so that anyone who later claims broader coverage has to
edit a test that states the limitation in writing.

### Neither representation claims to be clean

`redaction.complete` on the record and `evidence.redaction_complete` on the result
are both always `false`. Both read from one constant —
`shell.results.REDACTION_IS_COMPLETE`, which `shell.evidence` re-exports rather
than copying — so the two cannot drift into disagreeing. No input can flip either:
declaring every secret that appears in the output still leaves them `false`,
because completeness would be a claim about secrets nobody declared, which is
unknowable here.

This follows the precedent `Environment.network_enforced` set: a declared control
the code cannot actually deliver is reported as undelivered rather than implied.
A record that said nothing about redaction coverage would be read as clean.

## Storage

Records are written to `<source_root>/.shell/evidence/`, one JSON file per
operation, named `<recorded_at>-<record_id>.json` so a directory listing is in
chronological order.

**Anchored to the source root, not the work root.** `source_root` is trusted
control context; `work_root` is what model-driven operations may change. Evidence
goes in the former so an agent rewriting files in its workspace is not also
rewriting the record of what it did.

**That separation is real only when the deployment provides it.** When
`source_root` and `work_root` are the same directory — the common interactive case
— the store sits inside the writable tree and the ordering buys nothing. Every
record reports which case applied, as
`persistence.store_outside_work_root`, so a reader learns it from the payload
rather than from this paragraph.

One file per record rather than an appended log: pruning by age is a file listing
instead of a rewrite, and a write that fails partway corrupts nothing already
recorded. Writes are atomic — a temporary file in the destination directory,
moved into place — so a reader never observes a half-written record.

### A record cannot attest to its own write

The `persistence` block is part of the persisted body, so a reader off disk finds
every documented section. One field inside it is `null` there, and cannot be
otherwise: **`persisted` is not known when the bytes are serialized**, because the
record *is* what is being written.

| Field | On disk | On the returned record |
|---|---|---|
| `path` | the destination, computed before the write | the same path |
| `write_attempted` | `true` | `true` |
| `store_outside_work_root` | resolved | resolved |
| `persisted` | `null`, with `note` explaining why | `true` / `false` |
| `reason` | empty | the failure text, when one occurred |

Three options were available and two of them are dishonest. Omitting the block
leaves a reader silently missing a documented section — that was the previous
behaviour, and `shell operation show` tripped over it. Writing `persisted: true`
before knowing it is a claim about the future. Reporting `null` with a stated
reason is the same posture `execution.applied` and `effects.complete` take
elsewhere: a thing the code cannot know is reported as unknown.

**The gap is not closed by writing twice.** A second write, or an edit after the
first, would trade a documentation gap for a torn-file hazard — and the atomicity
above is worth more than filling in a field a reader can already infer. If you are
reading a record *from* a store, the write succeeded; that is what its presence
means, and `PERSISTENCE_UNKNOWN_NOTE` says so in the payload.

One consequence for validators: the on-disk body and the returned record have
different `persistence` blocks and therefore **different `integrity.content_sha256`
values**. That is correct — they are different bodies. Each digest validates
against the bytes it accompanies, which is the property that matters, and
`test_the_stored_bodys_digest_validates_against_its_own_bytes` pins it.

## Retention

`RetentionPolicy` bounds the store by count and by age. Defaults: 500 records,
14 days. Both bounds are non-`None` by default; an unbounded evidence store is a
disk-fill bug.

**Retention runs only when the store is written to.** There is no daemon, no
timer, and no process that visits an idle store — a consequence of staying
pure-stdlib with no background thread. A store that stops receiving records keeps
whatever it last held, indefinitely. `RetentionPolicy.to_dict()` reports this as
`enforced_on_write_only: true`, and a test pins that an idle store prunes nothing.

**Retention governs this store's directory only.** A record a consumer has already
read, copied, or forwarded to telemetry is beyond reach. Pruning is housekeeping,
never a deletion guarantee.

Pruning is best-effort: a file that cannot be removed is skipped, and failure to
tidy never propagates into the outcome of the operation being recorded.

## Degraded evidence

**An executed action that could not be recorded must never read as a clean run.**

When `capture()` is given a store and the write fails, it returns a result whose
`evidence.degraded` is `true` and whose `evidence.degraded_reason` names the
failure. The operation's own status is untouched — degraded evidence describes the
*record*, and must not rewrite the outcome of the work. A prior degradation reason
is appended to, never overwritten.

`capture()` never raises. The caller is recording something that already happened;
raising would replace a real outcome with an exception about the paperwork.

Two consequences worth stating:

- **The record cannot report its own failure to be written.** It is not on disk to
  be read. That is why the marker lives on the returned result, not only in
  `persistence`.
- **No store configured is not degraded.** The record was built and handed to its
  caller, which is a delivery channel. It is reported as
  `persistence.persisted: false` with a reason, and the result stays clean.

The failure path is also a redaction path: a declared secret appearing in a store
path or an OS error message is scrubbed from `degraded_reason` before it is
returned.

## Versioning

`schema_version` is readable from `Operation`, `OperationResult`, `Environment`,
and the evidence record — and from the persisted bytes, not only the in-memory
object. All four carry `shell.results.SCHEMA_VERSION` and version together,
because a consumer able to read one must be able to read all of them.

It is `"0"`: the pre-Milestone-1 generation, where the shapes are not yet stable.
Compare it for exact equality and treat anything unrecognized as incompatible.
Guessing at a shape change is how version skew becomes a security bug instead of
an error message.

## What this contract does not claim

- **It does not claim the effect list is complete.** `effects.complete` defaults
  to `false`. A host process may write anywhere it can reach, and nothing at this
  layer will enumerate that. Only a handler that performed every mutation itself
  and can name them all may set it true.
- **It does not claim redaction is complete.** See above; `redaction.complete` and
  `evidence.redaction_complete` are permanently `false`. Declared secrets are
  removed from both the record and the live result; undeclared ones are removed
  from neither.
- **It does not claim the stored body knows whether it was stored.**
  `persistence.persisted` is `null` on disk, by construction.
- **It does not claim the record is a security boundary.** An operation running on
  the host can reach the evidence directory like any other directory. Anchoring
  the store to the source root raises the bar for an accidental overwrite; it is
  not a control against a determined process, and host execution is a guard, not a
  sandbox.
- **It does not claim retention deletes anything beyond its own directory.**
- **It does not claim the digest validates the world.** `integrity.content_sha256`
  proves a record has not been altered since it was written. It cannot tell you
  whether the record was accurate when written.
