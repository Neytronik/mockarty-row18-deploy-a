# svc-a Kubernetes Deployment Marker

k8s-deploy-variant-A-ok

- Service: `svc-a`
- Delivery target: `k8s-prod-a`
- Environment: `stage`
- Acceptance surface: `http://127.0.0.1:18798/`
- Deployment owner: Mockarty Kubernetes deploy node
- Manifest policy: use the pre-existing manifest from the accepted commit; do not create or modify a manifest in this run.
