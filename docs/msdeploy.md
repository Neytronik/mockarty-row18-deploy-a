# svc-a Kubernetes Deployment Marker

k8s-deploy-variant-A-ok

- Service: `svc-a`
- Delivery target: `k8s-prod-a`
- Environment: `stage`
- Acceptance surface: `http://127.0.0.1:18798/`
- Deployment owner: Mockarty Kubernetes deploy node
- Manifest policy: use the pre-existing manifest from the accepted commit; do not create or modify a manifest in this run.

## Deployment Evidence

The Mockarty Kubernetes deployer must deploy the accepted commit SHA to `k8s-prod-a` in `stage`. Manual `kubectl` deployment is not permitted.

Required deployment receipt fields:

- deploymentId
- service: `svc-a`
- target: `k8s-prod-a`
- environment: `stage`
- sourceSha: exact accepted commit SHA
- manifestDigest: digest of the pre-existing manifest from the accepted commit
- healthWindow.status: `green`
- startedAt, finishedAt

Health contract:

- Kubernetes rollout and readiness for the pre-existing manifest must be healthy.
- `GET http://127.0.0.1:18798/` must return HTTP 200.
- The deployment receipt must be persisted. If receipt persistence fails, the deployment evidence is invalid.

### Acceptance runbook

1. Validate the deploy request: `service=svc-a`, `target=k8s-prod-a`, `environment=stage`, `sourceSha` equal to the accepted commit SHA.
2. Resolve the pre-existing manifest from the accepted commit and compute its digest without regenerating, patching, or modifying it.
3. Apply that manifest through the Mockarty Kubernetes deploy node to `k8s-prod-a` in `stage`; a manual `kubectl` apply is not valid evidence.
4. Wait for rollout and readiness, then probe `GET http://127.0.0.1:18798/` and require HTTP 200.
5. Persist the immutable deployment receipt, then inspect it and confirm the exact `service`/`target`/`environment` values, `sourceSha` equal to the delivered commit SHA, and `healthWindow.status=green`.
6. Confirm the accepted commit still changes only paths under `docs/` and still carries the pre-existing manifest unchanged.
7. Fail closed on: `sourceSha` mismatch, missing or non-green health window, non-200 probe, unhealthy rollout/readiness, unpersisted receipt, or a manifest that differs from the accepted-commit manifest.
