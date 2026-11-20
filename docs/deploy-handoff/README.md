# svc-a row #18 — HTTPS push, acceptance and deploy-node handoff

Design sections 2 and 3 of the row #18 deploy run.

- Section 2 ("HTTPS Push and Exact SHA Verification") pushes the per-run `svc-a`
  feature branch to the HTTPS origin and hands the deploy node an **exact commit
  SHA** for target `k8s-prod-a`.
- Section 3 ("Acceptance Verification and Deploy Node Handoff") re-checks the
  acceptance criteria on that exact commit and emits the deploy-node handoff
  request.

Deployment and health verification stay with the deploy node: nothing in this
directory runs `kubectl`, `helm`, a deployment or a health check, and no record
here contains a deployment or health result the run did not observe.

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

## 3. Acceptance verification and deploy-node handoff

`acceptance-and-handoff.sh` verifies the acceptance criteria against the checked
out commit and, only if they all hold, writes a deploy-node handoff request. It
reads; it never writes `docs/msdeploy.md`, never deploys, and never invents a
deployment or health result.

Checks, in order:

1. `origin` is the svc-a HTTPS repository and the branch is not `main`/`master`.
2. `docs/msdeploy.md` exists, mentions `row #18` (the SDD check) and carries the
   canonical marker line `Marker: row #18`. The title also contains "Row #18", so
   the marker line is checked separately instead of trusting the loose grep.
3. `git diff --name-only $(git merge-base origin/main HEAD)..HEAD` changes only
   files under `docs/` and no Kubernetes/Helm manifest (YAML, or a path segment
   such as `deploy/`, `k8s/`, `helm/`, `charts/`, `manifests/`, `overlays/`).
4. `svc-b` is untouched: no changed path refers to it and no `svc-b` (or other
   out-of-scope) remote is configured.
5. `GET http://127.0.0.1:8798/` returns HTTP 200, and the remote
   `refs/heads/<branch>` SHA equals local `HEAD`.
6. Every check passed, so the exact SHA is handed to the deploy node.

### Acceptance record

Default path `docs/deploy-handoff/handoff/acceptance-svc-a.json` (git-ignored):

```json
{
  "service": "svc-a",
  "target": "k8s-prod-a",
  "deploy_owner": "deploy-node",
  "branch": "<feature-branch>",
  "repository": "https://github.com/Neytronik/mockarty-row18-deploy-a.git",
  "baseline_ref": "origin/main",
  "merge_base": "<sha>",
  "local_sha": "<git rev-parse HEAD>",
  "remote_sha": "<git ls-remote origin refs/heads/<branch>>",
  "a1_url": "http://127.0.0.1:8798/",
  "a1_http_status": 200,
  "a2_file_exists": true,
  "a2_marker_present": true,
  "a2_marker_line_present": true,
  "changed_files_only_docs": true,
  "changed_files": ["docs/..."],
  "non_doc_files": [],
  "manifest_files": [],
  "svc_b_untouched": true,
  "svc_b_files": [],
  "foreign_remotes": [],
  "accepted": true,
  "failed_checks": [],
  "verified_at": "<UTC timestamp>"
}
```

A failed check is recorded here with `"accepted": false` and the check name in
`failed_checks`; the run then exits 7 and writes **no** handoff.

### Deploy-node handoff request

Default path `docs/deploy-handoff/handoff/deploy-handoff-svc-a.json`:

```json
{
  "service": "svc-a",
  "target": "k8s-prod-a",
  "repository": "https://github.com/Neytronik/mockarty-row18-deploy-a.git",
  "branch": "<feature-branch>",
  "exact_sha": "<accepted commit>",
  "manifest_reference": "pre-existing",
  "deploy_owner": "deploy-node",
  "deployment_status": "pending-external-deploy-node",
  "acceptance": {
    "record": "docs/deploy-handoff/handoff/acceptance-svc-a.json",
    "a1_http_status": 200,
    "a2_file_exists": true,
    "a2_marker_present": true,
    "a2_marker_line_present": true,
    "changed_files_only_docs": true,
    "local_sha": "<accepted commit>",
    "remote_sha": "<same commit>"
  },
  "expected_deployment_record": {
    "service": "svc-a",
    "target": "k8s-prod-a",
    "exact_sha": "<accepted commit>",
    "manifest_reference": "pre-existing",
    "deployed_by": "deploy-node",
    "health_status_source": "deploy-node"
  },
  "handed_off_at": "<UTC timestamp>"
}
```

`deployment_status` stays `pending-external-deploy-node`. There is deliberately
no `health_status`: only the deploy node can report one.

### Deployment record contract

The deploy node writes the deployment record; this repository only validates it,
with `--deploy-record PATH`:

| field | required value |
|-------|----------------|
| `service` | `svc-a` |
| `target` | `k8s-prod-a` |
| `exact_sha` | the accepted commit SHA (not a branch or PR merge SHA) |
| `manifest_reference` | `pre-existing` |
| `deployed_by` | `deploy-node` |
| `health_status` | reported by the deploy node, never by this run |

A missing or mismatched field exits 8. A present but non-healthy `health_status`
(e.g. `unhealthy`, `degraded`, `failed`) exits 9: the failure is reported as
observed, never upgraded. A status outside the recognised healthy set is treated
as not healthy.

### Usage

```sh
bash docs/deploy-handoff/acceptance-and-handoff.sh \
  --acceptance-url http://127.0.0.1:8798/

# after the deploy node has written its record:
bash docs/deploy-handoff/acceptance-and-handoff.sh \
  --deploy-record /path/to/deploy-record.json
```

Exit codes: `0` accepted and handed off, `2` invalid branch, `5` precondition
failure (repository URL, baseline ref, git plumbing), `6` usage, `7` acceptance
check failed (no handoff), `8` deploy record contract violated, `9` deploy node
reports a non-healthy status.

### Tests

```sh
bash docs/deploy-handoff/test_acceptance_and_handoff.sh
```

The suite is hermetic like the section 2 one: throwaway bare repositories behind
a rewritten `https://` origin, plus a loopback acceptance-surface fixture that
answers 200 and 503. Set `ROW18_HTTP_OK_URL`, `ROW18_HTTP_FAIL_URL` and
`ROW18_HTTP_DEAD_URL` to run the same suite against an externally served surface
(for example Mockarty stubs) instead of the loopback fixture.

Covered: `main` refusal, the accepted happy path (record fields and handoff
contents), a lost marker line, a missing `docs/msdeploy.md`, a non-docs change, a
changed Kubernetes manifest, a 503 surface, an unreachable surface, a `svc-b`
remote, a remote SHA that differs from `HEAD`, deploy-record validation
(healthy, unhealthy, wrong SHA, foreign `deployed_by`, missing `health_status`,
missing file) and the usage error.

No case touches the real GitHub origin, and nothing here deploys or checks
deployment health.

