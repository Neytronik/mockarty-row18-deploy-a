"""Row #18 variant B — svc-a exact-commit deployment and evidence handoff.

This module implements design section 2 ("Exact-Commit Deployment and Evidence
Handoff") for the svc-a delivery run.

It builds the ``DeployRequest`` from the exact commit that is delivered, submits
it to the Mockarty deploy node, measures a green post-deploy health window
against the acceptance URL, builds the ``DeployReceipt`` from the *observed*
results and only then stores the receipt.  Any failed check aborts before a
receipt exists — a green receipt is never fabricated.

The Mockarty deploy node is the deployer of record.  This module deliberately
has no cluster command path: it never runs ssh, kubectl, helm or any other
manual deployment tool.  The only subprocess it runs is ``git`` (read-only:
HEAD, origin URL, changed paths).

Standard library only.

Run with::

    python3 docs/deploy-handoff/deploy_handoff.py \
        --deploy-endpoint  http://127.0.0.1:5970/stubs/sandbox/row18/svc-a/variantB-<sha>/deployments \
        --receipt-store-endpoint http://127.0.0.1:5970/stubs/sandbox/row18/svc-a/variantB-<sha>/receipts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Delivery contract (row #18 variant B, svc-a)
# ---------------------------------------------------------------------------

SERVICE = "svc-a"
REPOSITORY = "https://github.com/Neytronik/mockarty-row18-deploy-a.git"
TARGET = "k8s-live-a"
ENVIRONMENT = "stage"
DEPLOYER = "mockarty-kubernetes-deployer"
ACCEPTANCE_URL = "http://127.0.0.1:18798/"
EXPECTED_STATUS = 200
HEALTH_METHOD = "GET"
MIN_CONSECUTIVE_PROBES = 3
PROBE_INTERVAL_SECONDS = 10
DOCS_SCOPE_PREFIX = "docs/"

#: Any of these tokens in an identity or a receipt means a non-Mockarty actor.
MANUAL_ACTOR_TOKENS = ("manual", "human", "kubectl", "helm", "ssh", "ansible")

#: Repositories/targets owned by the other delivery run.  They must never show
#: up in this run's receipt or deployment payload.
FOREIGN_SCOPE_TOKENS = ("svc-b", "mockarty-row18-deploy-b", "k8s-live-b")

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
MOCKARTY_ATTRIBUTION = re.compile(r"mockarty", re.IGNORECASE)

RECEIPT_REQUIRED_FIELDS = (
    "service",
    "repository",
    "commitSha",
    "target",
    "environment",
    "deployerIdentity",
    "healthWindow",
    "healthProbe",
    "status",
)

HEALTH_WINDOW_REQUIRED_FIELDS = (
    "start",
    "end",
    "durationSeconds",
    "intervalSeconds",
    "consecutiveSuccessfulProbes",
    "green",
)

HEALTH_PROBE_REQUIRED_FIELDS = (
    "method",
    "url",
    "expectedStatus",
    "observedStatus",
)

EXIT_OK = 0
EXIT_REQUEST_INVALID = 2
EXIT_HEALTH_FAILED = 3
EXIT_RECEIPT_INVALID = 4


class HandoffError(Exception):
    """Base class for handoff failures."""


class RequestError(HandoffError):
    """The DeployRequest is malformed or does not match the delivery contract."""


class DeployRefused(HandoffError):
    """The Mockarty deploy node refused the request or answered incoherently."""


class HealthWindowError(HandoffError):
    """The post-deploy health window is not green."""


class ReceiptError(HandoffError):
    """The receipt is incomplete or does not bind the delivered commit."""


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _require(condition: bool, message: str, error: type = RequestError) -> None:
    if not condition:
        raise error(message)


def _as_sha(value: Any, field: str = "commitSha") -> str:
    """Return a validated 40-character lowercase commit SHA."""
    _require(
        isinstance(value, str) and bool(value),
        f"{field} is missing; the exact 40-character commit SHA is required",
    )
    assert isinstance(value, str)  # for type checkers
    _require(
        bool(SHA_PATTERN.match(value)),
        f"{field} {value!r} is not a 40-character lowercase Git SHA",
    )
    return value


def is_mockarty_attributable(identity: Any) -> bool:
    """True only for an identity that names Mockarty and no manual actor."""
    if not isinstance(identity, str) or not identity.strip():
        return False
    if not MOCKARTY_ATTRIBUTION.search(identity):
        return False
    lowered = identity.lower()
    return not any(token in lowered for token in MANUAL_ACTOR_TOKENS)


def _json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _reject_foreign_scope(value: Any, what: str) -> None:
    text = _json_text(value).lower()
    for token in FOREIGN_SCOPE_TOKENS:
        _require(
            token.lower() not in text,
            f"{what} mentions {token!r}; svc-b and k8s-live-b belong to the other run",
        )


# ---------------------------------------------------------------------------
# DeployRequest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployRequest:
    """The commit-pinned deployment request handed to the Mockarty deploy node."""

    service: str
    repository: str
    commit_sha: str
    target: str
    environment: str
    deployer: str

    def to_payload(self) -> Dict[str, str]:
        return {
            "service": self.service,
            "repository": self.repository,
            "commitSha": self.commit_sha,
            "target": self.target,
            "environment": self.environment,
            "deployer": self.deployer,
        }

    def idempotency_key(self) -> str:
        material = "\0".join((self.service, self.commit_sha, self.target))
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def build_deploy_request(
    delivered_sha: str,
    *,
    service: str = SERVICE,
    repository: str = REPOSITORY,
    target: str = TARGET,
    environment: str = ENVIRONMENT,
    deployer: str = DEPLOYER,
) -> DeployRequest:
    """Build and locally validate a DeployRequest.  Never makes a network call."""
    commit_sha = _as_sha(delivered_sha)
    _require(
        service == SERVICE,
        f"service {service!r} is out of scope; this run delivers {SERVICE!r}",
    )
    _require(
        repository.rstrip("/") == REPOSITORY.rstrip("/"),
        f"repository {repository!r} is out of scope; expected {REPOSITORY!r}",
    )
    _require(
        target == TARGET,
        f"target {target!r} does not match the delivery target {TARGET!r}",
    )
    _require(
        environment == ENVIRONMENT,
        f"environment {environment!r} does not match {ENVIRONMENT!r}",
    )
    _require(
        deployer == DEPLOYER,
        f"deployer {deployer!r} is not the Mockarty deploy node {DEPLOYER!r}",
    )
    request = DeployRequest(
        service=service,
        repository=repository,
        commit_sha=commit_sha,
        target=target,
        environment=environment,
        deployer=deployer,
    )
    _reject_foreign_scope(request.to_payload(), "DeployRequest")
    return request


def assert_delivered_sha(commit_sha: str, delivered_sha: str) -> None:
    """The requested SHA must be exactly the SHA that was delivered."""
    _as_sha(commit_sha)
    _as_sha(delivered_sha, "deliveredSha")
    _require(
        commit_sha == delivered_sha,
        f"requested commit {commit_sha} does not match delivered commit {delivered_sha}",
    )


# ---------------------------------------------------------------------------
# Repository scope (read-only git)
# ---------------------------------------------------------------------------


def _git(repo_dir: str, *args: str) -> str:
    """Run one read-only git command.  The only subprocess in this module."""
    completed = subprocess.run(
        ["git", "-C", repo_dir, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def check_repository_scope(repo_dir: str = ".", base: Optional[str] = None) -> Dict[str, Any]:
    """Evidence for the repository-isolation and docs-only constraints (R1, R2)."""
    head = _git(repo_dir, "rev-parse", "HEAD")
    origin = _git(repo_dir, "remote", "get-url", "origin")
    changed: List[str] = []
    if base:
        _as_sha(base, "base")
        changed = [
            line.strip()
            for line in _git(repo_dir, "diff", "--name-only", f"{base}...HEAD").splitlines()
            if line.strip()
        ]
    non_docs = [path for path in changed if not path.startswith(DOCS_SCOPE_PREFIX)]
    problems: List[str] = []
    if origin.rstrip("/") != REPOSITORY.rstrip("/"):
        problems.append(f"origin {origin} is not the svc-a repository {REPOSITORY}")
    if non_docs:
        problems.append("changed paths outside docs/: " + ", ".join(non_docs))
    return {
        "head": head,
        "origin": origin,
        "base": base,
        "changedPaths": changed,
        "docsOnly": not non_docs,
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def _urlopen(request: urllib.request.Request, timeout: float = 15.0) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


Opener = Callable[..., Any]


def _read_response(response: Any) -> Tuple[int, str]:
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    raw = response.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return int(status), raw


def _parse_json(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"body": parsed}


def _post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    opener: Optional[Opener] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 15.0,
) -> Tuple[int, Dict[str, Any]]:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **dict(headers or {}),
        },
    )
    transport = opener or _urlopen
    try:
        response = transport(request, timeout=timeout)
    except urllib.error.HTTPError as exc:  # a refusal is a result, not a crash
        status = int(exc.code)
        raw = exc.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return status, _parse_json(raw)
    if hasattr(response, "__enter__"):
        with response as entered:
            status, raw = _read_response(entered)
    else:  # simple fakes used by the tests
        status, raw = _read_response(response)
    return status, _parse_json(raw)


def _get_json(
    url: str, *, opener: Optional[Opener] = None, timeout: float = 15.0
) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    transport = opener or _urlopen
    try:
        response = transport(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return int(exc.code), _parse_json(raw)
    if hasattr(response, "__enter__"):
        with response as entered:
            status, raw = _read_response(entered)
    else:
        status, raw = _read_response(response)
    return status, _parse_json(raw)


# ---------------------------------------------------------------------------
# Step 1 — submit the exact commit to the Mockarty deploy node
# ---------------------------------------------------------------------------


def submit_deploy_request(
    request: DeployRequest,
    endpoint: str,
    *,
    opener: Optional[Opener] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """POST the DeployRequest; the answer must describe the same commit/target."""
    _require(bool(endpoint), "deploy node endpoint is required")
    status, payload = _post_json(
        endpoint,
        request.to_payload(),
        opener=opener,
        headers={"Idempotency-Key": request.idempotency_key()},
        timeout=timeout,
    )
    if status not in (200, 202):
        raise DeployRefused(
            f"deploy node answered HTTP {status}: {payload.get('reason', payload)}"
        )
    _require(
        payload.get("accepted") is True,
        f"deploy node did not accept the request: {payload}",
        DeployRefused,
    )
    _require(
        payload.get("commitSha") == request.commit_sha,
        "deploy node answered for a different commit "
        f"({payload.get('commitSha')!r} != {request.commit_sha!r})",
        DeployRefused,
    )
    _require(
        payload.get("target") == request.target,
        f"deploy node answered for target {payload.get('target')!r}",
        DeployRefused,
    )
    _require(
        payload.get("environment") == request.environment,
        f"deploy node answered for environment {payload.get('environment')!r}",
        DeployRefused,
    )
    _require(
        payload.get("manualDeploy") is False,
        "deploy node answered with a manual deployment flag",
        DeployRefused,
    )
    identity = payload.get("deployerIdentity") or payload.get("actor")
    _require(
        is_mockarty_attributable(identity),
        f"deployerIdentity {identity!r} is not attributable to the Mockarty deploy node",
        DeployRefused,
    )
    payload = dict(payload)
    payload["deployerIdentity"] = identity
    _reject_foreign_scope(payload, "deploy node response")
    return payload


# ---------------------------------------------------------------------------
# Step 2 — measure the post-deploy health window
# ---------------------------------------------------------------------------


def http_status(url: str, *, opener: Optional[Opener] = None, timeout: float = 5.0) -> int:
    """One HTTP probe.  Returns the status code, or raises HealthWindowError."""
    request = urllib.request.Request(url, method=HEALTH_METHOD)
    transport = opener or _urlopen
    try:
        response = transport(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise HealthWindowError(f"probe {url} failed: {exc}") from exc
    if hasattr(response, "__enter__"):
        with response as entered:
            status, _ = _read_response(entered)
            return status
    status, _ = _read_response(response)
    return status


def run_health_window(
    url: str = ACCEPTANCE_URL,
    *,
    probes: int = MIN_CONSECUTIVE_PROBES,
    interval: float = PROBE_INTERVAL_SECONDS,
    probe_fn: Optional[Callable[[str], int]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """Probe ``url`` until ``probes`` consecutive 200s are observed.

    The window stops at the first failure: an intermittent error leaves the
    window red and no receipt may be written.
    """
    _require(
        probes >= MIN_CONSECUTIVE_PROBES,
        f"a green window needs at least {MIN_CONSECUTIVE_PROBES} consecutive "
        f"probes, {probes} requested",
        HealthWindowError,
    )
    _require(interval > 0, "probe interval must be positive", HealthWindowError)
    probe = probe_fn or (lambda target: http_status(target))
    wait = sleep or time.sleep
    monotonic = clock or time.monotonic

    started_at = _now_iso()
    began = monotonic()
    statuses: List[int] = []
    failures: List[str] = []
    consecutive = 0

    for attempt in range(probes):
        try:
            status = int(probe(url))
        except HandoffError as exc:
            failures.append(str(exc))
            break
        except Exception as exc:  # transport errors are probe failures too
            failures.append(f"probe {url} raised {type(exc).__name__}: {exc}")
            break
        statuses.append(status)
        if status != EXPECTED_STATUS:
            failures.append(f"probe {attempt + 1} observed HTTP {status}, expected {EXPECTED_STATUS}")
            consecutive = 0
            break
        consecutive += 1
        if attempt < probes - 1:
            wait(interval)

    ended_at = _now_iso()
    duration = round(monotonic() - began, 3)
    green = (
        not failures
        and consecutive >= MIN_CONSECUTIVE_PROBES
        and len(statuses) == probes
        and all(status == EXPECTED_STATUS for status in statuses)
    )
    return {
        "start": started_at,
        "end": ended_at,
        "durationSeconds": duration,
        "intervalSeconds": interval,
        "probesRequested": probes,
        "probesAttempted": len(statuses),
        "consecutiveSuccessfulProbes": consecutive,
        "observedStatuses": statuses,
        "failures": failures,
        "green": green,
    }


# ---------------------------------------------------------------------------
# Step 3 — build, validate and store the receipt
# ---------------------------------------------------------------------------


def build_receipt(
    request: DeployRequest,
    deployment: Mapping[str, Any],
    health_window: Mapping[str, Any],
    *,
    acceptance_url: str = ACCEPTANCE_URL,
) -> Dict[str, Any]:
    """Assemble the receipt from what was actually observed."""
    statuses = list(health_window.get("observedStatuses") or [])
    observed = statuses[-1] if statuses else None
    green = bool(health_window.get("green")) and observed == EXPECTED_STATUS
    receipt = {
        "schema": "row18.deploy_receipt/v1",
        "service": request.service,
        "repository": request.repository,
        "commitSha": request.commit_sha,
        "target": request.target,
        "environment": request.environment,
        "deployer": request.deployer,
        "deployerIdentity": deployment.get("deployerIdentity"),
        "deploymentId": deployment.get("deploymentId"),
        "manualDeploy": deployment.get("manualDeploy"),
        "healthWindow": {
            "start": health_window.get("start"),
            "end": health_window.get("end"),
            "durationSeconds": health_window.get("durationSeconds"),
            "intervalSeconds": health_window.get("intervalSeconds"),
            "probesRequested": health_window.get("probesRequested"),
            "probesAttempted": health_window.get("probesAttempted"),
            "consecutiveSuccessfulProbes": health_window.get("consecutiveSuccessfulProbes"),
            "observedStatuses": statuses,
            "failures": list(health_window.get("failures") or []),
            "green": green,
        },
        "healthProbe": {
            "method": HEALTH_METHOD,
            "url": acceptance_url,
            "expectedStatus": EXPECTED_STATUS,
            "observedStatus": observed,
        },
        "status": "green" if green else "red",
        "writtenAt": _now_iso(),
    }
    return receipt


def validate_receipt(receipt: Mapping[str, Any], delivered_sha: str) -> None:
    """Fail closed unless the receipt binds this exact commit and a green window."""
    _as_sha(delivered_sha, "deliveredSha")
    _require(isinstance(receipt, Mapping), "receipt is not an object", ReceiptError)
    for field_name in RECEIPT_REQUIRED_FIELDS:
        _require(
            receipt.get(field_name) not in (None, "", {}, []),
            f"receipt is missing required field {field_name!r}",
            ReceiptError,
        )

    _require(receipt.get("service") == SERVICE, f"receipt service is {receipt.get('service')!r}", ReceiptError)
    _require(
        str(receipt.get("repository", "")).rstrip("/") == REPOSITORY.rstrip("/"),
        f"receipt repository is {receipt.get('repository')!r}",
        ReceiptError,
    )
    _require(receipt.get("target") == TARGET, f"receipt target is {receipt.get('target')!r}", ReceiptError)
    _require(
        receipt.get("environment") == ENVIRONMENT,
        f"receipt environment is {receipt.get('environment')!r}",
        ReceiptError,
    )
    commit_sha = str(receipt.get("commitSha") or "")
    _require(
        bool(SHA_PATTERN.match(commit_sha)),
        f"receipt commitSha {commit_sha!r} is not a 40-character lowercase Git SHA",
        ReceiptError,
    )
    _require(
        commit_sha == delivered_sha,
        f"receipt commitSha {commit_sha} does not match delivered commit {delivered_sha}",
        ReceiptError,
    )
    _require(
        is_mockarty_attributable(receipt.get("deployerIdentity")),
        f"receipt deployerIdentity {receipt.get('deployerIdentity')!r} is not "
        "attributable to the Mockarty deploy node",
        ReceiptError,
    )
    _require(
        receipt.get("manualDeploy") in (False, None),
        "receipt records a manual deployment",
        ReceiptError,
    )

    window = receipt.get("healthWindow")
    _require(isinstance(window, Mapping), "receipt healthWindow is not an object", ReceiptError)
    for field_name in HEALTH_WINDOW_REQUIRED_FIELDS:
        _require(
            window.get(field_name) not in (None, ""),
            f"healthWindow is missing required field {field_name!r}",
            ReceiptError,
        )
    _require(window.get("green") is True, "healthWindow is not green", ReceiptError)
    try:
        duration = float(window.get("durationSeconds"))
        interval = float(window.get("intervalSeconds"))
        consecutive = int(window.get("consecutiveSuccessfulProbes"))
    except (TypeError, ValueError) as exc:
        raise ReceiptError(f"healthWindow has non-numeric measurements: {exc}") from exc
    _require(duration > 0, "healthWindow durationSeconds must be positive", ReceiptError)
    _require(interval > 0, "healthWindow intervalSeconds must be positive", ReceiptError)
    _require(
        consecutive >= MIN_CONSECUTIVE_PROBES,
        f"healthWindow has {consecutive} consecutive successful probes; "
        f"{MIN_CONSECUTIVE_PROBES} required",
        ReceiptError,
    )
    observed_statuses = window.get("observedStatuses") or []
    _require(
        len(observed_statuses) >= consecutive,
        "healthWindow observedStatuses is shorter than its consecutive count",
        ReceiptError,
    )
    _require(
        all(int(status) == EXPECTED_STATUS for status in observed_statuses),
        f"healthWindow observed non-200 statuses: {observed_statuses}",
        ReceiptError,
    )
    _require(
        not window.get("failures"),
        f"healthWindow recorded probe failures: {window.get('failures')}",
        ReceiptError,
    )

    probe = receipt.get("healthProbe")
    _require(isinstance(probe, Mapping), "receipt healthProbe is not an object", ReceiptError)
    for field_name in HEALTH_PROBE_REQUIRED_FIELDS:
        _require(
            probe.get(field_name) not in (None, ""),
            f"healthProbe is missing required field {field_name!r}",
            ReceiptError,
        )
    _require(probe.get("method") == HEALTH_METHOD, f"healthProbe method is {probe.get('method')!r}", ReceiptError)
    _require(
        probe.get("url") == ACCEPTANCE_URL,
        f"healthProbe url is {probe.get('url')!r}, expected {ACCEPTANCE_URL!r}",
        ReceiptError,
    )
    _require(
        int(probe.get("expectedStatus")) == EXPECTED_STATUS,
        f"healthProbe expectedStatus is {probe.get('expectedStatus')!r}",
        ReceiptError,
    )
    _require(
        int(probe.get("observedStatus")) == EXPECTED_STATUS,
        f"healthProbe observed HTTP {probe.get('observedStatus')!r}",
        ReceiptError,
    )
    _require(receipt.get("status") == "green", f"receipt status is {receipt.get('status')!r}", ReceiptError)
    _reject_foreign_scope(receipt, "receipt")


def store_receipt(
    receipt: Mapping[str, Any],
    endpoint: str,
    *,
    opener: Optional[Opener] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Append the validated receipt to the evidence store."""
    _require(bool(endpoint), "receipt store endpoint is required", ReceiptError)
    status, payload = _post_json(endpoint, receipt, opener=opener, timeout=timeout)
    if status not in (200, 201):
        raise ReceiptError(f"receipt store answered HTTP {status}: {payload}")
    _require(
        payload.get("stored") is True,
        f"receipt store did not confirm storage: {payload}",
        ReceiptError,
    )
    return payload


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def verify(
    *,
    delivered_sha: str,
    deploy_endpoint: str,
    receipt_store_endpoint: str,
    probe_url: str = ACCEPTANCE_URL,
    probes: int = MIN_CONSECUTIVE_PROBES,
    interval: float = PROBE_INTERVAL_SECONDS,
    probe_fn: Optional[Callable[[str], int]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    opener: Optional[Opener] = None,
) -> Dict[str, Any]:
    """Run the whole handoff and return the stored green receipt.

    Order is contractual: request → deploy → health window → receipt →
    validation → storage.  A red window raises before any receipt is written.
    """
    request = build_deploy_request(delivered_sha)
    deployment = submit_deploy_request(request, deploy_endpoint, opener=opener)
    window = run_health_window(
        probe_url,
        probes=probes,
        interval=interval,
        probe_fn=probe_fn,
        sleep=sleep,
        clock=clock,
    )
    if not window["green"]:
        raise HealthWindowError(
            "post-deploy health window is not green: "
            + ("; ".join(window["failures"]) or "incomplete window")
        )
    receipt = build_receipt(request, deployment, window, acceptance_url=probe_url)
    validate_receipt(receipt, delivered_sha)
    stored = store_receipt(receipt, receipt_store_endpoint, opener=opener)
    receipt["evidenceStore"] = {
        "endpoint": receipt_store_endpoint,
        "receiptId": stored.get("receiptId"),
        "storedAt": stored.get("storedAt") or receipt["writtenAt"],
    }
    return receipt


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--commit-sha", default=None, help="defaults to git rev-parse HEAD")
    parser.add_argument("--base", default=None, help="base commit for the docs-only scope check")
    parser.add_argument("--deploy-endpoint", default=None)
    parser.add_argument("--receipt-store-endpoint", default=None)
    parser.add_argument("--probe-url", default=ACCEPTANCE_URL)
    parser.add_argument("--probes", type=int, default=MIN_CONSECUTIVE_PROBES)
    parser.add_argument("--interval", type=float, default=PROBE_INTERVAL_SECONDS)
    parser.add_argument("--receipt-out", default=None, help="write the receipt JSON here")
    parser.add_argument("--dry-run", action="store_true", help="validate the request only")
    args = parser.parse_args(argv)

    try:
        head = _git(args.repo_dir, "rev-parse", "HEAD")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(json.dumps({"ok": False, "error": f"cannot read git HEAD: {exc}"}))
        return EXIT_REQUEST_INVALID

    delivered_sha = args.commit_sha or head
    scope = check_repository_scope(args.repo_dir, args.base)
    try:
        assert_delivered_sha(delivered_sha, head)
        request = build_deploy_request(delivered_sha)
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "scope": scope}, indent=2))
        return EXIT_REQUEST_INVALID

    if scope["problems"]:
        print(json.dumps({"ok": False, "error": "repository scope violation", "scope": scope}, indent=2))
        return EXIT_REQUEST_INVALID

    if args.dry_run:
        print(json.dumps({"ok": True, "dryRun": True, "request": request.to_payload(), "scope": scope}, indent=2))
        return EXIT_OK

    if not args.deploy_endpoint or not args.receipt_store_endpoint:
        print(json.dumps({"ok": False, "error": "deploy and receipt-store endpoints are required"}))
        return EXIT_REQUEST_INVALID

    try:
        receipt = verify(
            delivered_sha=delivered_sha,
            deploy_endpoint=args.deploy_endpoint,
            receipt_store_endpoint=args.receipt_store_endpoint,
            probe_url=args.probe_url,
            probes=args.probes,
            interval=args.interval,
        )
    except HealthWindowError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "status": "red"}, indent=2))
        return EXIT_HEALTH_FAILED
    except ReceiptError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return EXIT_RECEIPT_INVALID
    except HandoffError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return EXIT_REQUEST_INVALID

    if args.receipt_out:
        with open(args.receipt_out, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2, sort_keys=True)
            handle.write("\n")

    print(json.dumps({"ok": True, "request": request.to_payload(), "scope": scope, "receipt": receipt}, indent=2))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
