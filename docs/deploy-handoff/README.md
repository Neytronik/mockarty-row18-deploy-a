# svc-a row #18 — HTTPS push and exact SHA handoff

Design section 2 of the row #18 deploy run ("HTTPS Push and Exact SHA
Verification"). It pushes the per-run `svc-a` feature branch to the HTTPS origin
and hands the deploy node an **exact commit SHA** for target `k8s-prod-a`.
Deployment and health verification stay with the deploy node.

## Deploy handoff record

`push-and-verify.sh` writes this record (default path
`docs/deploy-handoff/handoff/svc-a.json`, generated and git-ignored — a committed
file cannot contain its own commit SHA):

```json
{
  "service": "svc-a",
  "target": "k8s-prod-a",
  "repository": "https://github.com/Neytronik/mockarty-row18-deploy-a.git",
  "branch": "<feature-branch>",
  "exact_sha": "<40-or-64-hex-commit>",
  "manifest_reference": "pre-existing",
  "verification": {
    "remote": "origin",
    "ref": "refs/heads/<feature-branch>",
    "local_sha": "<git rev-parse HEAD>",
    "remote_sha": "<git ls-remote origin refs/heads/<feature-branch>>",
    "sha_match": true,
    "verified_at": "<UTC timestamp>"
  }
}
```

`exact_sha` is the only deployment reference. The branch name is traceability
metadata — the deploy node must never deploy a moving branch name or a PR merge
SHA.

## Behaviour

1. Assert the branch is not `main`/`master` (and HEAD is not detached).
2. Assert `origin` is the expected `https://` svc-a repository.
3. Assert no uncommitted tracked changes, so `HEAD` contains the whole change.
4. If `refs/heads/<branch>` already exists remotely at a different SHA, stop:
   use a unique branch instead of force-pushing.
5. `git push --set-upstream origin refs/heads/<branch>:refs/heads/<branch>`
   (no `--force`, no `--force-with-lease`).
6. Capture `git rev-parse HEAD`; read `git ls-remote origin
   refs/heads/<branch>`, keeping only the line whose ref is exactly
   `refs/heads/<branch>`.
7. Compare the two SHAs. On mismatch re-query once, then fail without writing
   the record.
8. Write the handoff record atomically.

Exit codes: `0` verified, `2` invalid branch, `3` push rejected/failed, `4` SHA
mismatch after re-query, `5` precondition failure, `6` usage.

## Usage

```sh
bash docs/deploy-handoff/push-and-verify.sh --out docs/deploy-handoff/handoff/svc-a.json
```

The record lands in an ignored directory so the accepted branch diff stays
documentation-only.

## Tests

```sh
bash docs/deploy-handoff/test_push_and_verify.sh
```

The suite is hermetic: each case builds a throwaway bare repository, points the
work clone's `origin` at a fake `https://` URL rewritten with
`url.<file>.insteadOf`, and covers

- refusal to push `main`,
- happy path (push, exact-ref SHA equality, record contents),
- idempotent re-run on an already-pushed identical branch,
- divergence detection without a force-push,
- `ls-remote` filtering when decoy branches exist,
- persistent SHA mismatch (one re-query, then exit 4, no record),
- rejected push (exit 3, nothing pushed, no record),
- non-HTTPS/foreign repository guard.

No case touches the real GitHub origin and nothing in this directory deploys or
checks deployment health.
