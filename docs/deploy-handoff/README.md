# Row #18 variant B — svc-a exact-commit deployment and evidence handoff

Implementation of design section 2 ("Exact-Commit Deployment and Evidence
Handoff") for the svc-a delivery run (target `k8s-live-a`, environment `stage`).

The section changes no product code: it pins the accepted `docs/msdeploy.md`
commit, hands that exact SHA to the Mockarty deploy node, measures a green
post-deploy health window against the acceptance surface and records a receipt.
This directory holds the handoff client and its tests; the receipt itself lives
in the delivery evidence store, not in Git (see "Why the receipt is not
committed" below).

## DeployRequest

| field | value |
| --- | --- |
| `service` | `svc-a` |
| `repository` | `https://github.com/Neytronik/mockarty-row18-deploy-a.git` |
| `commitSha` | exact 40-character lowercase SHA of the delivered commit |
| `target` | `k8s-live-a` |
| `environment` | `stage` |
| `deployer` | `mockarty-kubernetes-deployer` |

`POST` is sent with `Idempotency-Key: sha256:` + SHA-256 of
`service \0 commitSha \0 target`, so a retry can never deploy a second time.

## DeployReceipt

| field | rule |
| --- | --- |
| `service` | `svc-a` |
| `repository` | the svc-a repository |
| `commitSha` | must equal `git rev-parse HEAD` of the delivered repository |
| `target` / `environment` | `k8s-live-a` / `stage` |
| `deployerIdentity` | names Mockarty and no manual actor (`manual`, `human`, `kubectl`, `helm`, `ssh`, `ansible`) |
| `healthWindow` | `start`, `end`, `durationSeconds > 0`, `intervalSeconds`, `consecutiveSuccessfulProbes >= 3`, observed statuses, no failures, `green: true` |
| `healthProbe` | `GET http://127.0.0.1:18798/`, expected `200`, observed `200` |
| `status` | `green` only when every check above passes |
| `manualDeploy` | `false` |

Green post-deploy health window: **at least three consecutive successful `GET`
probes to `http://127.0.0.1:18798/` at 10-second intervals**, every response
`200`, and no probe failure inside the window. The window stops at the first
failure; an intermittent error leaves it red.

## Behaviour

`verify()` runs one contractual order:

1. `build_deploy_request()` validates the allowlist and the 40-character SHA
   locally — no network call when the request is invalid.
2. `submit_deploy_request()` posts to the deploy node and refuses an answer that
   names another commit/target/environment, a manual deploy flag, or a
   non-Mockarty identity.
3. `run_health_window()` probes the acceptance surface.
4. `build_receipt()` derives the receipt from the *observed* probes.
5. `validate_receipt()` fails closed on any missing field or mismatch.
6. `store_receipt()` appends the receipt to the evidence store.

A red window raises `HealthWindowError` **before** the receipt store is called —
no green receipt can be produced by a failed deployment.

## Usage

Dry run (validates the request and the repository scope, no network):

```sh
python3 docs/deploy-handoff/deploy_handoff.py --repo-dir . --dry-run \
  --base <base-commit>
```

Live handoff against the deploy node (endpoints from the Mockarty mocks below;
the CLI resolves `--commit-sha` from `git rev-parse HEAD` by default):

```sh
python3 docs/deploy-handoff/deploy_handoff.py --repo-dir . \
  --base <base-commit> \
  --deploy-endpoint http://127.0.0.1:5970/stubs/sandbox/row18/svc-a/variantB-<sha7>/deployments \
  --receipt-store-endpoint http://127.0.0.1:5970/stubs/sandbox/row18/svc-a/variantB-<sha7>/receipts \
  --receipt-out /tmp/row18-svc-a-receipt.json
```

Exit codes: `0` green receipt stored, `2` request/scope refused, `3` health
window not green, `4` receipt invalid or unstored.

## Fail-closed edge cases

| edge case | outcome |
| --- | --- |
| missing / non-40-character `commitSha` | `RequestError`, no network call |
| SHA != delivered `HEAD` | `RequestError` |
| service / repository / target / environment mismatch | `RequestError` |
| deployer not the Mockarty deploy node | `RequestError` |
| manual ssh / kubectl / helm actor | refusal (`DeployRefused` / `ReceiptError`) |
| non-2xx deploy answer, or an answer for another commit/target | `DeployRefused` |
| HTTP status other than `200` | red window, `HealthWindowError` |
| intermittent probe failure | red window, `HealthWindowError` |
| window shorter than 3 consecutive probes | `HealthWindowError` |
| receipt missing a required field | `ReceiptError` |
| writes outside `docs/`, or any svc-b / k8s-live-b evidence | scope violation / `ReceiptError` |

## Why the receipt is not committed

A receipt must name the commit that was deployed. Committing the receipt moves
`HEAD`, so the committed receipt would immediately name a commit that is no
longer the delivered one. The receipt is therefore written to the delivery
evidence store **after** the last commit, and review compares its `commitSha`
with `git rev-parse HEAD` of the delivered branch.

## Mockarty artifacts used for engine verification

- Mock `row18-svc-a-variantB-<sha7>-deploy-node` — `POST .../deployments`
  (deploy-node stand-in; accepts only the exact SHA, `k8s-live-a`, `stage`).
- Mock `row18-svc-a-variantB-<sha7>-receipt-store` — `POST .../receipts`.
- Mock `row18-svc-a-variantB-<sha7>-receipt-latest` — `GET .../receipts/latest`.
- Mock `row18-svc-a-variantB-<sha7>-deploy-manual-reject` — manual deploy
  negative control (`422`).
- Mock `row18-svc-a-variantB-<sha7>-acceptance` / `-probe-fail` — acceptance
  surface `200` and probe-failure negative control (`503`).
- Mock `row18-svc-a-variantB-<sha7>-msdeploy` — served `docs/msdeploy.md`.
- API Tester collection + Test Plan — `Row #18 variant B — svc-a deploy
  handoff`.

Live Kubernetes execution is not advertised as a tool in the delivery session,
so the deploy-node execution face is a Mockarty mock; the health window is
measured against the real loopback acceptance surface at
`http://127.0.0.1:18798/`. The receipt records both facts
(`deployerIdentity`, `healthProbe`, `healthWindow`), so review can see exactly
what was executed.

## Tests

```sh
python3 -m unittest discover -s docs/deploy-handoff -p 'test_*.py' -v
```

43 tests: request validation and SHA pinning, out-of-scope service/repository/
target/environment/deployer, repository isolation and docs-only diff against a
real temporary git repository, git-only subprocess guard, deploy-node refusals,
green/red/intermittent/short health windows, every required receipt field and
health-window/health-probe field, manual-actor and foreign-repository evidence,
the ordered end-to-end flow (including "no receipt after a red window"), and the
CLI dry run.
