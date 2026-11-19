# Row #18 deployment request contract and allowlist gate

Implementation of design section 3 (svc-a run). The orchestrator turns a
`verified` `RunManifest` into a commit-pinned `DeploymentRequest`, enforces the
run allowlist locally, and submits it to the Mockarty deploy node's
`POST /deployments`. The deploy node is the only deployment executor; this code
has no cluster command path.

## Contract

`DeploymentRequest` (`POST /deployments`):

```json
{
  "service": "svc-a",
  "repoUrl": "https://github.com/Neytronik/mockarty-row18-deploy-a.git",
  "branch": "feature/row18-deploy-a",
  "commitSha": "f91910dfcf6c238763596251487c07ac41c9e378",
  "target": "k8s-live-a",
  "cluster": "Robolance stage",
  "namespace": "mockarty-deploy-live",
  "healthWindowSeconds": 300,
  "idempotencyKey": "sha256:<runId + commitSha + target>"
}
```

`DeploymentResponse`: `{ runId, status: accepted|duplicate|rejected, message }`.
`rejected` responses carry a 4xx status and an empty `runId`; they are terminal.

## Allowlist gate

Accepted only when every field matches:

| field | required value |
| --- | --- |
| `service` | `svc-a` |
| `repoUrl` | `https://github.com/Neytronik/mockarty-row18-deploy-a.git` |
| `branch` | `feature/row18-deploy-a` |
| `target` | `k8s-live-a` |
| `cluster` | `Robolance stage` |
| `namespace` | `mockarty-deploy-live` |
| `commitSha` | 40-character lowercase Git SHA recorded by the verified manifest |
| `healthWindowSeconds` | positive integer |
| `idempotencyKey` | non-empty, derived by the client |

`svc-b`, `k8s-live-b`, the svc-b repository, any other branch, cluster,
namespace, or a missing/unverified SHA fails closed **before** any HTTP call.

## Behaviour

1. `submit_manifest` refuses a manifest whose status is not `verified` and a
   SHA that is missing, short, or different from the recorded manifest SHA.
2. `build_request` validates the allowlist locally and derives
   `idempotencyKey = "sha256:" + sha256(runId \0 commitSha \0 target)`.
3. `DeployClient.submit` re-validates, then posts. 2xx `accepted` is a new run;
   2xx `duplicate` returns the existing run (same `runId`).
4. 5xx and timeouts retry with exponential backoff up to `max_attempts`, always
   with the same payload/idempotency key. 4xx is surfaced without retry.
5. The result (`runId`, `status`, `message`, `attempts`) is returned for
   persistence; no cluster command is spawned anywhere in the module.

## Usage

```sh
python3 docs/deploy-gate/deploy_gate.py \
  --manifest run-manifest.json \
  --endpoint http://<deploy-node>/deployments
```

`run-manifest.json` is the `RunManifest` with `status: "verified"`.

## Tests

```sh
python3 -m unittest discover -s docs/deploy-gate -p 'test_*.py' -v
```

Covers accepted svc-a, svc-b/k8s-live-b rejection, wrong repo/branch/cluster/
namespace rejection, missing/short/unverified SHA rejection, non-positive health
window, duplicate replay returning the same `runId`, 5xx/timeout retry with one
key, 4xx non-retry, and a source guard proving no manual cluster command path.

## Mockarty artifacts used for engine verification

- Mock `row18-svc-a-deployments` — `POST /deployments` allowlist + idempotency
  gate (sandbox namespace), with `X-Simulate-First-Status` for transient 5xx.
- Mock `row18-svc-a-deploy-runs` — `GET /row18/svc-a/deploy-runs` evidence.
- Collection `Row #18 deploy gate — POST /deployments allowlist + idempotency
  (clean)` (`93777cf5-4776-414e-9b5b-d4dac5090e9f`).
- Environment `row18-deploy-gate` (deploy node stub URL, allowlisted fields).
