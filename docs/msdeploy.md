# svc-a deployment marker

row18-k8s-deploy-live-ok service=svc-a target=k8s-live-a

## Run scope

This file is the only artifact produced by the documentation run for `svc-a`.
The run follows the immutable input tuple below and nothing else:

| Input | Accepted value |
|-------|----------------|
| repoUrl | `https://github.com/Neytronik/mockarty-row18-deploy-a.git` |
| branch | the branch supplied to the run, used verbatim |
| service | `svc-a` |
| target | `k8s-live-a` |
| markerToken | `row18-k8s-deploy-live-ok` |

## Guardrails

- The provided branch must be non-empty and is used exactly as supplied. The
  run does not create, switch to, or push any other branch.
- A wrong repository URL, service name, target, marker token, or an empty
  branch aborts the run before the workspace is modified.
- Any operation naming another service or another repository aborts the run.
  This repository is the only one in scope.
- Any attempted write outside `docs/msdeploy.md` aborts the run before commit,
  so tests, application code, CI configuration, scripts, git configuration and
  other paths stay untouched.
- The commit created on the provided branch is the immutable handoff artifact.
- No `kubectl` invocation and no manual deployment: the Mockarty deploy node
  deploys the accepted commit to `k8s-live-a` and owns the post-deploy health
  window.
