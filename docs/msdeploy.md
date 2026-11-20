# Row #18 Deployment Documentation

Service: svc-a
Delivery target: k8s-prod-a
Repository URL: https://github.com/Neytronik/mockarty-row18-deploy-a.git
Change scope: documentation-only

Marker: row #18

This change is documentation-only. Deployment of `svc-a` to `k8s-prod-a` is
owned by the deploy node and uses the pre-existing manifest in `deploy/app.yaml`;
no manual `kubectl` or `helm` operation is performed from this repository.
