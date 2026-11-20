# svc-a row #18 — frozen accepted SHA

This is the external evidence area for the acceptance gate
(`docs/acceptance-gate/checks/svc-a-row18/gate.py`). It is written only when the
gate accepts a candidate commit; it is never written on rejection.

## Frozen acceptance

- service: `svc-a`
- target: `k8s-prod-a`
- repository: `https://github.com/Neytronik/mockarty-row18-deploy-a.git`
- accepted_sha: `5c459c09278961dfe6cbf5729ee318c859dda71e`
- marker: `docs/msdeploy.md` matched `row[[:space:]#-]*18`
- changed paths: `docs/msdeploy.md`
- actor: `deploy-node`

`deployment-request.txt` is the exact deploy request handed to the deploy node:

```text
service=svc-a
target=k8s-prod-a
accepted_sha=5c459c09278961dfe6cbf5729ee318c859dda71e
actor=deploy-node
```

## Files

- `accepted_sha.json` — the freeze record, written once. A later candidate cannot
  replace this SHA; the gate rejects with `ACCEPTED_SHA_ALREADY_FROZEN`.
- `deployment-request.txt` — the deploy request contract.
- `submission.json` — the recorded handoff to the deploy-node intake
  (`POST /row18/svc-a/deployments`, HTTP 202 accepted, idempotency key
  `sha256:64a1ce0f…`). Reproduced by Mockarty mock
  `row18-svc-a-accepted-sha-gate` and collection
  `svc-a row #18 accepted_sha freeze + deploy request contract (5c459c0)`.
- `rejections.jsonl` — absent until an acceptance failure is recorded; each line
  carries the reason code, the reason, the candidate SHA and
  `deploy_request_emitted: false`.

## Regenerate / verify

```bash
python3 docs/acceptance-gate/checks/svc-a-row18/gate.py \
  --repo . \
  --candidate 5c459c09278961dfe6cbf5729ee318c859dda71e \
  --evidence-dir docs/evidence/accepted_sha/svc-a

python3 docs/acceptance-gate/checks/svc-a-row18/gate.py \
  --validate-request docs/evidence/accepted_sha/svc-a/deployment-request.txt
```

The candidate is read with `git archive`; the gate does not mutate the service
repository, svc-b or shared deploy configuration.
