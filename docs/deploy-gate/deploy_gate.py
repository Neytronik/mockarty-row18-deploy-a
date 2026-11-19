"""Row #18 deployment request contract and allowlist gate.

This module is the row18-orchestrator deploy client. It turns a verified
``RunManifest`` into a commit-pinned ``DeploymentRequest``, enforces the run
allowlist strictly *before* any network call, derives an idempotency key from
``runId + commitSha + target`` and submits the request to the Mockarty deploy
node's ``POST /deployments`` endpoint.

The module never runs cluster tooling: the Mockarty deploy node is the only
deployment executor. It deliberately has no shell-out path (see the guard test).

Standard library only, so the gate can run inside the bare orchestrator image.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Run allowlist (row #18, svc-a only)
# ---------------------------------------------------------------------------

ALLOWED_SERVICE = "svc-a"
ALLOWED_REPO_URL = "https://github.com/Neytronik/mockarty-row18-deploy-a.git"
ALLOWED_BRANCH = "feature/row18-deploy-a"
ALLOWED_TARGET = "k8s-live-a"
ALLOWED_CLUSTER = "Robolance stage"
ALLOWED_NAMESPACE = "mockarty-deploy-live"

ALLOWLIST = {
    "service": ALLOWED_SERVICE,
    "repoUrl": ALLOWED_REPO_URL,
    "branch": ALLOWED_BRANCH,
    "target": ALLOWED_TARGET,
    "cluster": ALLOWED_CLUSTER,
    "namespace": ALLOWED_NAMESPACE,
}

# Any payload mentioning one of these identities belongs to the other run.
OUT_OF_SCOPE_TOKENS = (
    "svc-b",
    "k8s-live-b",
    "mockarty-row18-deploy-b",
)

RESPONSE_STATUSES = ("accepted", "duplicate", "rejected")

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class GateError(Exception):
    """Base class for gate failures."""


class AllowlistError(GateError):
    """The request is outside the run allowlist; no network call was made."""


class ManifestError(GateError):
    """The RunManifest is not deployable (wrong status or bad SHA)."""


class DeployRejected(GateError):
    """The deploy node rejected the request with a 4xx; do not retry."""

    def __init__(self, status_code: int, body: Mapping[str, Any]):
        self.status_code = status_code
        self.body = dict(body)
        message = body.get("message") or "deploy node rejected the request"
        super().__init__("HTTP %s: %s" % (status_code, message))


class DeployRetryExhausted(GateError):
    """Retries (timeout/5xx) were exhausted with the same idempotency key."""

    def __init__(self, attempts: int, last_error: str):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            "deploy submission failed after %d attempt(s): %s" % (attempts, last_error)
        )


class TransportError(GateError):
    """Network-level failure (timeout, connection reset); retryable."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunManifest:
    """Run-scoped manifest produced by the git verification section."""

    run_id: str
    service: str
    repo_url: str
    branch: str
    commit_sha: str
    target: str
    cluster: str
    namespace: str
    health_window_seconds: int
    status: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunManifest":
        return cls(
            run_id=str(data.get("runId", "")),
            service=str(data.get("service", "")),
            repo_url=str(data.get("repoUrl", "")),
            branch=str(data.get("branch", "")),
            commit_sha=str(data.get("commitSha", "")),
            target=str(data.get("target", "")),
            cluster=str(data.get("cluster", "")),
            namespace=str(data.get("namespace", "")),
            health_window_seconds=_as_int(data.get("healthWindowSeconds", 0)),
            status=str(data.get("status", "")),
        )


@dataclass(frozen=True)
class DeploymentRequest:
    """Commit-pinned request accepted by ``POST /deployments``."""

    service: str
    repo_url: str
    branch: str
    commit_sha: str
    target: str
    cluster: str
    namespace: str
    health_window_seconds: int
    idempotency_key: str

    def to_payload(self) -> dict:
        return {
            "service": self.service,
            "repoUrl": self.repo_url,
            "branch": self.branch,
            "commitSha": self.commit_sha,
            "target": self.target,
            "cluster": self.cluster,
            "namespace": self.namespace,
            "healthWindowSeconds": self.health_window_seconds,
            "idempotencyKey": self.idempotency_key,
        }


@dataclass(frozen=True)
class DeploymentResult:
    """Persisted outcome of a deploy-node submission."""

    run_id: str
    status: str
    message: str
    attempts: int

    def to_dict(self) -> dict:
        return {
            "runId": self.run_id,
            "status": self.status,
            "message": self.message,
            "attempts": self.attempts,
        }


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Allowlist validation
# ---------------------------------------------------------------------------


def idempotency_key(run_id: str, commit_sha: str, target: str) -> str:
    """Derive a stable key from runId + commitSha + target."""
    material = "%s\x00%s\x00%s" % (run_id, commit_sha, target)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "sha256:" + digest


def validate_allowlist(request: DeploymentRequest) -> None:
    """Raise AllowlistError unless every field matches the run allowlist.

    Purely local: callers must invoke this before any HTTP request.
    """
    actual = {
        "service": request.service,
        "repoUrl": request.repo_url,
        "branch": request.branch,
        "target": request.target,
        "cluster": request.cluster,
        "namespace": request.namespace,
    }
    violations = []
    for field, expected in ALLOWLIST.items():
        if actual[field] != expected:
            violations.append(
                "%s must be %r, got %r" % (field, expected, actual[field])
            )
    if not SHA_PATTERN.match(request.commit_sha or ""):
        violations.append(
            "commitSha must be the full 40-character lowercase Git SHA verified "
            "by the previous section"
        )
    if request.health_window_seconds <= 0:
        violations.append("healthWindowSeconds must be a positive integer")
    if not request.idempotency_key:
        violations.append("idempotencyKey is required")

    blob = json.dumps(request.to_payload()).lower()
    for token in OUT_OF_SCOPE_TOKENS:
        if token in blob:
            violations.append("out-of-scope identity present: %s" % token)

    if violations:
        raise AllowlistError("; ".join(violations))


def build_request(manifest: RunManifest, commit_sha: Optional[str] = None) -> DeploymentRequest:
    """Build the DeploymentRequest from a verified RunManifest.

    ``commit_sha`` lets the caller pass the exact SHA recorded by the git
    verifier; it must equal the manifest SHA, otherwise the request is refused.
    """
    if manifest.status != "verified":
        raise ManifestError(
            "RunManifest status must be 'verified' before deployment, got %r"
            % manifest.status
        )
    sha = manifest.commit_sha
    if commit_sha is not None and commit_sha != sha:
        raise ManifestError(
            "commitSha mismatch: manifest records %r but submission carries %r"
            % (sha, commit_sha)
        )
    if not SHA_PATTERN.match(sha or ""):
        raise ManifestError("commitSha is missing or not a 40-character Git SHA")
    if manifest.health_window_seconds <= 0:
        raise ManifestError("healthWindowSeconds must be a positive integer")

    request = DeploymentRequest(
        service=manifest.service,
        repo_url=manifest.repo_url,
        branch=manifest.branch,
        commit_sha=sha,
        target=manifest.target,
        cluster=manifest.cluster,
        namespace=manifest.namespace,
        health_window_seconds=manifest.health_window_seconds,
        idempotency_key=idempotency_key(manifest.run_id, sha, manifest.target),
    )
    validate_allowlist(request)
    return request


# ---------------------------------------------------------------------------
# Transport and client
# ---------------------------------------------------------------------------

Transport = Callable[[str, Mapping[str, Any], float], Tuple[int, Mapping[str, Any]]]


def urllib_transport(
    url: str, payload: Mapping[str, Any], timeout: float
) -> Tuple[int, Mapping[str, Any]]:
    """POST JSON to the deploy node. Raises TransportError on network failure."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a JSON body
        return exc.code, _decode(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(str(exc)) from exc


def _decode(raw: bytes) -> Mapping[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


class DeployClient:
    """Submits DeploymentRequest objects to the Mockarty deploy node."""

    def __init__(
        self,
        endpoint: str,
        transport: Optional[Transport] = None,
        max_attempts: int = 3,
        timeout: float = 10.0,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.endpoint = endpoint
        self._transport = transport or urllib_transport
        self.max_attempts = max_attempts
        self.timeout = timeout
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def submit(self, request: DeploymentRequest) -> DeploymentResult:
        # Fail closed: allowlist runs before the first network attempt.
        validate_allowlist(request)

        payload = request.to_payload()
        last_error = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            try:
                status_code, body = self._transport(self.endpoint, payload, self.timeout)
            except TransportError as exc:
                last_error = "transport error: %s" % exc
            else:
                if 200 <= status_code < 300:
                    return self._result(body, attempt)
                if 400 <= status_code < 500:
                    # Validation errors are terminal: never retry blindly.
                    raise DeployRejected(status_code, body)
                last_error = "HTTP %s: %s" % (
                    status_code,
                    body.get("message", "server error"),
                )

            if attempt == self.max_attempts:
                break
            self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))

        raise DeployRetryExhausted(self.max_attempts, last_error)

    @staticmethod
    def _result(body: Mapping[str, Any], attempt: int) -> DeploymentResult:
        run_id = body.get("runId")
        status = body.get("status")
        if not isinstance(run_id, str) or not run_id:
            raise GateError("deploy node response is missing runId")
        if status not in RESPONSE_STATUSES:
            raise GateError(
                "deploy node response status must be one of %s, got %r"
                % ("/".join(RESPONSE_STATUSES), status)
            )
        if status == "rejected":
            raise DeployRejected(400, body)
        message = body.get("message")
        return DeploymentResult(
            run_id=run_id,
            status=status,
            message=message if isinstance(message, str) else "",
            attempts=attempt,
        )


# ---------------------------------------------------------------------------
# Orchestrator entry point
# ---------------------------------------------------------------------------


def submit_manifest(
    manifest: RunManifest,
    client: DeployClient,
    commit_sha: Optional[str] = None,
) -> DeploymentResult:
    """Validate a verified manifest and submit it to the deploy node.

    This is the whole orchestrator path: no cluster commands, no fallback
    executor, and retries reuse the same request (and idempotency key).
    """
    request = build_request(manifest, commit_sha=commit_sha)
    return client.submit(request)


def _main(argv: Optional[list] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Submit a verified row #18 RunManifest to the deploy node."
    )
    parser.add_argument("--manifest", required=True, help="path to RunManifest JSON")
    parser.add_argument("--endpoint", required=True, help="POST /deployments URL")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--backoff", type=float, default=0.5)
    args = parser.parse_args(argv)

    with open(args.manifest, "r", encoding="utf-8") as handle:
        manifest = RunManifest.from_dict(json.load(handle))

    client = DeployClient(
        args.endpoint,
        max_attempts=args.max_attempts,
        timeout=args.timeout,
        backoff_seconds=args.backoff,
    )
    try:
        result = submit_manifest(manifest, client)
    except GateError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1

    print(json.dumps(result.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
