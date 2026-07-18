# Threat model

**shell-cli ships a guard, not a sandbox.**

This document exists because the package name invites the wrong assumption. A
library whose headline is *safe shell execution* reads as an isolation boundary.
It is not one, it has never been one, and no amount of hardening short of
namespaces/containers/seccomp would make it one. State that plainly rather than
letting a reader infer otherwise.

## Status

The six primitives, the path confinement, and the approval policy are **not yet
extracted** from [`colleague`](https://github.com/agentculture/colleague) (see
[issue #1](https://github.com/agentculture/shell-cli/issues/1)). This document
describes the contract the extraction must uphold — the posture is inherited
along with the code, and must not be quietly upgraded in transit.

## Who this protects, and from what

The asset is the operator's machine and repository. The actor is **a language
model driving a tool loop** — one that is capable, fallible, and occasionally
confidently wrong.

| Protected against | Not protected against |
|---|---|
| A model that misreads a path and walks out of the repo root | A model (or a prompt injection steering it) that is *trying* to escape |
| A write into a tree the operator declared read-only | Anything at all once the command reaches a shell |
| A command the operator's policy has not approved | Obfuscation that hides the command's real target |
| A result large enough to blow the caller's context window | Resource exhaustion (CPU, disk, fork bombs) |

The distinction that matters: this stops **accidental and careless** behaviour.
It does not stop **adversarial** behaviour, and it is not a security boundary
you should place untrusted input behind.

## Known bypasses

These are not bugs to be fixed. They are consequences of gating a string that a
shell will later re-interpret, and they are documented rather than papered over:

- **`sh -c` and friends** — the gate inspects the command it is handed, not
  what that command subsequently spawns.
- **Shell expansion** — variable expansion, command substitution, globbing, and
  concatenation can assemble a blocked target out of pieces that individually
  look fine.
- **Here-docs and pipelines** — the payload is not the token the gate examined.
- **Interpreters** — `python -c`, `perl -e`, and any other language runtime take
  arbitrary code as an opaque argument.
- **Symlinks and TOCTOU** — path confinement resolves at check time; a path can
  change between the check and the operation.

Upstream in colleague, the token-aware `shlex` check is deliberately paired with
a conservative substring fallback for unparseable commands, and the guard is
written to never raise — a guard that crashes the drive is worse than one that
is merely incomplete. Preserve both properties.

## What actual isolation would require

Real isolation is separate, deliberate work — not a wording change and not an
incremental hardening of the existing guard:

- OS-level containment (namespaces, cgroups, seccomp), or
- a container runtime, or
- a virtual machine.

A container/VM execution backend is planned. When it lands, the two claims must
stay clearly separated in every surface that mentions them: *the host-side guard
remains best-effort* and *the isolated backend is the one that actually
contains*. One sentence must never cover both.

## Enforcement

`tests/test_honesty.py` asserts the disclaimer is present in `learn`, the
`explain` root, `explain safety`, the README, `CLAUDE.md`, and this file — and
that no shipped surface makes a positive isolation claim. If you find that test
inconvenient, the correct response is to change the wording, not the test.
