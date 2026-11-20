# svc-a row #18 — Accepted SHA gate

This is the acceptance gate from the SDD section *Accepted SHA Freeze and Deploy
Request Contract*. It is the boundary between the svc-a documentation commit and
the deploy node: deployment may start only when this gate accepts a candidate
commit and freezes a full `accepted_sha`.

The gate lives under `docs/` because that is the only write scope this run may
publish (`docs/` in the deploy envelope). It is an external interface: it reads
the service repository and writes only to the evidence directory it is given.

## Contract

Input:

- repository `https://github.com/Neytronik/mockarty-row18-deploy-a.git`
- service `svc-a`
- target `k8s-prod-a`
- candidate commit SHA (full 40-character git SHA)

Accepted output (written to `evidence/accepted_sha/svc-a/deployment-request.txt`):

```text
service=svc-a
target=k8s-prod-a
accepted_sha=<full immutable git commit SHA>
actor=deploy-node
```

No `accepted_sha` means no deploy request. A request that is missing
`accepted_sha`, carries a short/foreign SHA, or names another service, target or
actor is rejected before it can reach the deploy node.

## Checks

| Check | Rejection code | Rule |
|---|---|---|
| Repository | `REPOSITORY_MISMATCH` | `origin` normalises to the svc-a https repository |
| SHA shape | `CANDIDATE_SHA_NOT_FULL` | the candidate is a full 40-character lowercase commit SHA |
| Commit exists | `CANDIDATE_COMMIT_NOT_FOUND` | the SHA resolves to a commit in the repository |
| Marker file | `MARKER_FILE_MISSING` | `docs/msdeploy.md` exists at the candidate |
| Marker | `MARKER_MISSING` | the file matches `row[[:space:]#-]*18` |
| Service | `SERVICE_MARKER_MISSING` | the file names `svc-a` |
| Target | `TARGET_MARKER_MISSING` | the file names `k8s-prod-a` |
| Scope | `OUT_OF_SCOPE_CHANGE` | the commit changes only paths under `docs/` |
| Freeze | `ACCEPTED_SHA_ALREADY_FROZEN` | an existing `accepted_sha` is never replaced |

The candidate is materialised with `git archive` into a temporary directory, so
the gate never creates a worktree, checks nothing out in place, and never
modifies the service repository. It never calls kubectl or helm: delivery
belongs to the deploy node.

The change-scope diff is the candidate commit itself (`git diff-tree --root`).
Pass `--base <sha>` to scope a range instead.

## Usage

```bash
python3 docs/acceptance-gate/checks/svc-a-row18/gate.py \
  --repo . \
  --candidate 5c459c09278961dfe6cbf5729ee318c859dda71e \
  --evidence-dir docs/evidence/accepted_sha/svc-a
```

- exit `0` — accepted, evidence frozen, request emitted
- exit `2` — rejected, reason recorded, no request emitted
- exit `3` — acceptance passed but the optional deploy-node handoff failed

Validate an existing request file:

```bash
python3 docs/acceptance-gate/checks/svc-a-row18/gate.py \
  --validate-request docs/evidence/accepted_sha/svc-a/deployment-request.txt
```

Optional handoff (the deploy node still owns delivery and its health check):

```bash
python3 docs/acceptance-gate/checks/svc-a-row18/gate.py ... \
  --submit-url http://127.0.0.1:5970/stubs/sandbox/row18/svc-a/deployments
```

The handoff POSTs the request as JSON with a deterministic `Idempotency-Key`
(`sha256(service|target|accepted_sha)`), records `submission.json`, and is only
attempted after acceptance passed. A rejected acceptance never calls it.

## Evidence

`docs/evidence/accepted_sha/svc-a/` is the external evidence area:

- `accepted_sha.json` — the frozen record (service, target, repository,
  accepted_sha, changed paths, marker line, checks, `frozen_at`). Written once;
  a later candidate cannot replace it.
- `deployment-request.txt` — the exact deploy request handed to the deploy node.
- `rejections.jsonl` — append-only rejection records with the reason code, the
  full reason, the candidate SHA and `deploy_request_emitted: false`.
- `submission.json` — present only when a handoff was attempted.

## Tests

```bash
python3 -m unittest discover \
  -s docs/acceptance-gate/checks/svc-a-row18 -p 'test_*.py' -v
```

The suite covers the design's pass and fail cases against disposable git
repositories: marker present and SHA frozen; missing `docs/msdeploy.md` with no
deploy request; missing marker with a recorded reason; a deploy request without
`accepted_sha` rejected; out-of-scope changes, a foreign repository and a short
SHA rejected; an already-frozen SHA not replaced; a rejected acceptance never
calling the deploy node; and the source repository left untouched.
