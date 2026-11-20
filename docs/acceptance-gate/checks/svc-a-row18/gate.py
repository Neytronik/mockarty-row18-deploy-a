#!/usr/bin/env python3
"""Row #18 acceptance gate for svc-a: freeze ``accepted_sha`` and emit a deploy request.

The gate is the boundary between a candidate svc-a documentation commit and the
deploy node.  It turns a verified commit into an immutable deploy input:

    service=svc-a
    target=k8s-prod-a
    accepted_sha=<full immutable git commit SHA>
    actor=deploy-node

Design rules implemented here (SDD section "Accepted SHA Freeze and Deploy
Request Contract"):

* the repository must be the svc-a https repository;
* the candidate must be a full git commit SHA and is materialised read-only;
* ``docs/msdeploy.md`` must exist and match ``row[[:space:]#-]*18``;
* the file must name ``svc-a`` and ``k8s-prod-a``;
* the commit itself must not change anything outside the documentation scope;
* a full ``accepted_sha`` is frozen in the evidence area;
* a request without ``accepted_sha`` is never valid;
* on failure the reason is recorded and no deploy request is emitted.

The source repository is never mutated: the candidate is read with
``git archive`` into a temporary directory and the only writes go to the
explicit ``--evidence-dir``.  The gate never calls kubectl/helm; delivery to
Kubernetes belongs to the deploy node.

Standard library only, so the gate runs in a bare CI image.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# --- Fixed contract -------------------------------------------------------

SERVICE = "svc-a"
TARGET = "k8s-prod-a"
ACTOR = "deploy-node"
REPOSITORY = "https://github.com/Neytronik/mockarty-row18-deploy-a.git"

MARKER_PATH = "docs/msdeploy.md"
# POSIX ERE form from the design; the Python engine spells [[:space:]] as \s.
MARKER_REGEX_POSIX = "row[[:space:]#-]*18"
MARKER_RE = re.compile(r"row[\s#-]*18", re.IGNORECASE)
# The run's declared documentation scope (deploy envelope scope: ["docs/"]).
ALLOWED_SCOPE_PREFIX = "docs/"

SERVICE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9-])svc-a(?![A-Za-z0-9-])")
TARGET_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9-])k8s-prod-a(?![A-Za-z0-9-])")

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

EVIDENCE_SCHEMA = "row18.accepted_sha/v1"
REJECTION_SCHEMA = "row18.accepted_sha.rejection/v1"
SUBMISSION_SCHEMA = "row18.deploy_request.submission/v1"

ACCEPTED_FILE = "accepted_sha.json"
REQUEST_FILE = "deployment-request.txt"
REJECTION_FILE = "rejections.jsonl"
SUBMISSION_FILE = "submission.json"

# --- Result model ---------------------------------------------------------

CHECK_CODES = {
    "repository_is_svc_a": "REPOSITORY_MISMATCH",
    "candidate_sha_is_full": "CANDIDATE_SHA_NOT_FULL",
    "candidate_commit_exists": "CANDIDATE_COMMIT_NOT_FOUND",
    "marker_file_present": "MARKER_FILE_MISSING",
    "marker_present": "MARKER_MISSING",
    "service_named": "SERVICE_MARKER_MISSING",
    "target_named": "TARGET_MARKER_MISSING",
    "documentation_scope": "OUT_OF_SCOPE_CHANGE",
    "accepted_sha_frozen": "ACCEPTED_SHA_ALREADY_FROZEN",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    code: str = ""

    def __post_init__(self) -> None:
        if not self.code:
            self.code = CHECK_CODES.get(self.name, "CHECK_FAILED")

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": "pass" if self.ok else "fail",
            "detail": self.detail,
        }
        if not self.ok:
            payload["reject_code"] = self.code
        return payload


@dataclass
class AcceptanceResult:
    checks: list[Check]
    accepted_sha: str = ""
    changed_paths: list[str] = field(default_factory=list)
    marker_line: str = ""

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    @property
    def accepted(self) -> bool:
        return not self.failed

    @property
    def reason_code(self) -> str:
        return self.failed[0].code if self.failed else ""

    @property
    def reason(self) -> str:
        if not self.failed:
            return ""
        return "; ".join(f"{check.code}: {check.detail}" for check in self.failed)


class GateError(Exception):
    """A git/IO problem that prevents a reliable verdict."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


# --- Deploy request contract ---------------------------------------------


@dataclass(frozen=True)
class DeployRequest:
    service: str
    target: str
    accepted_sha: str
    actor: str

    KEYS = ("service", "target", "accepted_sha", "actor")

    def as_dict(self) -> dict[str, str]:
        return {
            "service": self.service,
            "target": self.target,
            "accepted_sha": self.accepted_sha,
            "actor": self.actor,
        }

    def to_text(self) -> str:
        return "".join(f"{key}={getattr(self, key)}\n" for key in self.KEYS)

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.service != SERVICE:
            errors.append(f"service must be {SERVICE!r}, got {self.service!r}")
        if self.target != TARGET:
            errors.append(f"target must be {TARGET!r}, got {self.target!r}")
        if self.actor != ACTOR:
            errors.append(f"actor must be {ACTOR!r}, got {self.actor!r}")
        if not self.accepted_sha:
            errors.append(
                "accepted_sha is required; no deploy request may be emitted without a frozen accepted_sha"
            )
        elif not FULL_SHA_RE.match(self.accepted_sha):
            errors.append(
                "accepted_sha must be the full 40-character lowercase git commit SHA, "
                f"got {self.accepted_sha!r}"
            )
        return errors

    def validate(self) -> "DeployRequest":
        errors = self.validation_errors()
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _parse_request_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    duplicates: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"deploy request line is not key=value: {line!r}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in values:
            duplicates.append(key)
        values[key] = value
    if duplicates:
        raise ValueError("deploy request repeats field(s): " + ", ".join(sorted(set(duplicates))))
    missing = [key for key in DeployRequest.KEYS if key not in values]
    if missing:
        prefix = "accepted_sha is required; " if "accepted_sha" in missing else ""
        raise ValueError(
            prefix + "deploy request is missing required field(s): " + ", ".join(missing)
        )
    extra = [key for key in values if key not in DeployRequest.KEYS]
    if extra:
        raise ValueError("deploy request contains unexpected field(s): " + ", ".join(sorted(extra)))
    return values


def parse_deploy_request(text: str) -> DeployRequest:
    """Parse the contract text; raises ValueError for a malformed request."""
    return DeployRequest(**_parse_request_lines(text)).validate()


def validate_deploy_request(document: str | Mapping[str, Any]) -> list[str]:
    """Return every contract violation; an empty list means the request is valid."""
    if isinstance(document, Mapping):
        values = {key: str(document.get(key, "")) for key in DeployRequest.KEYS}
        extra = [key for key in document if key not in DeployRequest.KEYS]
        errors = DeployRequest(**values).validation_errors()
        if extra:
            errors.append("deploy request contains unexpected field(s): " + ", ".join(sorted(extra)))
        return errors
    try:
        parse_deploy_request(str(document))
    except ValueError as exc:
        return [part.strip() for part in str(exc).split(";") if part.strip()]
    return []


def build_deploy_request(accepted_sha: str) -> DeployRequest:
    """Build the only deploy request this gate may emit."""
    return DeployRequest(SERVICE, TARGET, accepted_sha, ACTOR).validate()


# --- Git helpers ----------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if check and completed.returncode != 0:
        raise GateError(
            "GIT_ERROR",
            f"git {' '.join(args)} failed ({completed.returncode}): {completed.stderr.strip()}",
        )
    return completed


def normalize_remote(url: str) -> str:
    """Normalise https/ssh/scp git remotes to ``https://host/path`` without credentials."""
    value = url.strip().rstrip("/")
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<path>.+)$", value)
    if match:
        host, path = match.group("host"), match.group("path")
    else:
        scp = re.match(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$", value)
        if not scp:
            return value.lower().removesuffix(".git")
        host, path = scp.group("host"), scp.group("path")
    if path.endswith(".git"):
        path = path[:-4]
    return f"https://{host.lower()}/{path}".rstrip("/")


def repository_origin(repo: Path) -> str:
    completed = _git(repo, "config", "--get", "remote.origin.url", check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def resolve_commit(repo: Path, candidate: str) -> str:
    completed = _git(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}", check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip().splitlines()[0].strip()


def _extract_archive(repo: Path, sha: str) -> tempfile.TemporaryDirectory[str]:
    """Materialise the candidate commit read-only, without touching the checkout."""
    completed = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        capture_output=True,
    )
    if completed.returncode != 0:
        raise GateError(
            "CHECKOUT_FAILED",
            f"git archive {sha} failed: {completed.stderr.decode('utf-8', 'replace').strip()}",
        )
    tempdir = tempfile.TemporaryDirectory(prefix="row18-acceptance-")
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            try:
                archive.extractall(tempdir.name, filter="data")  # Python 3.12+
            except TypeError:  # pragma: no cover - older interpreters
                archive.extractall(tempdir.name)
    except (tarfile.TarError, OSError) as exc:
        tempdir.cleanup()
        raise GateError("CHECKOUT_FAILED", f"could not materialise {sha}: {exc}") from exc
    return tempdir


def commit_changed_paths(repo: Path, sha: str, base: str | None = None) -> list[str]:
    if base:
        completed = _git(repo, "diff", "--name-only", "--no-renames", "-z", base, sha)
    else:
        completed = _git(
            repo,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "--no-renames",
            "--root",
            "-z",
            sha,
        )
    return [path for path in completed.stdout.split("\0") if path]


# --- Acceptance evaluation ------------------------------------------------


def evaluate(
    repo: Path,
    candidate: str,
    base: str | None = None,
) -> AcceptanceResult:
    """Evaluate the contract without writing anything."""
    checks: list[Check] = []
    result = AcceptanceResult(checks=checks)

    origin = normalize_remote(repository_origin(repo))
    expected = normalize_remote(REPOSITORY)
    checks.append(
        Check(
            "repository_is_svc_a",
            origin == expected,
            f"origin is {origin or '<unset>'}, expected {expected}",
        )
    )
    if not checks[-1].ok:
        return result

    candidate = candidate.strip()
    checks.append(
        Check(
            "candidate_sha_is_full",
            bool(FULL_SHA_RE.match(candidate)),
            f"candidate {candidate or '<empty>'!r} must be the full 40-character lowercase commit SHA",
        )
    )
    if not checks[-1].ok:
        return result

    resolved = resolve_commit(repo, candidate)
    checks.append(
        Check(
            "candidate_commit_exists",
            bool(resolved),
            f"commit {candidate} resolves to {resolved or '<missing>'}",
        )
    )
    if not checks[-1].ok:
        return result
    if resolved != candidate:
        checks.append(
            Check(
                "candidate_sha_is_full",
                False,
                f"candidate {candidate} is not the full commit identity; resolved {resolved}",
            )
        )
        return result
    result.accepted_sha = resolved

    tree = _extract_archive(repo, resolved)
    try:
        marker_path = Path(tree.name) / MARKER_PATH
        marker_exists = marker_path.is_file()
        checks.append(
            Check(
                "marker_file_present",
                marker_exists,
                f"{MARKER_PATH} {'found' if marker_exists else 'is missing'} in the checkout of {resolved}",
            )
        )

        text = ""
        if marker_exists:
            try:
                text = marker_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                checks.append(
                    Check("marker_present", False, f"{MARKER_PATH} is not valid UTF-8 text")
                )
            else:
                match = MARKER_RE.search(text)
                checks.append(
                    Check(
                        "marker_present",
                        bool(match),
                        f"{MARKER_PATH} {'matches' if match else 'does not match'} {MARKER_REGEX_POSIX}",
                    )
                )
                if match:
                    for line in text.splitlines():
                        if MARKER_RE.search(line):
                            result.marker_line = line.strip()
                            break
                checks.append(
                    Check(
                        "service_named",
                        bool(SERVICE_TOKEN_RE.search(text)),
                        f"{MARKER_PATH} {'names' if SERVICE_TOKEN_RE.search(text) else 'does not name'} {SERVICE}",
                    )
                )
                checks.append(
                    Check(
                        "target_named",
                        bool(TARGET_TOKEN_RE.search(text)),
                        f"{MARKER_PATH} {'names' if TARGET_TOKEN_RE.search(text) else 'does not name'} {TARGET}",
                    )
                )

        paths = commit_changed_paths(repo, resolved, base)
        result.changed_paths = paths
        outside = [path for path in paths if not path.startswith(ALLOWED_SCOPE_PREFIX)]
        if not paths:
            checks.append(
                Check(
                    "documentation_scope",
                    False,
                    f"candidate commit {resolved} changes no files; a documentation change is required",
                )
            )
        elif outside:
            checks.append(
                Check(
                    "documentation_scope",
                    False,
                    "candidate commit changes path(s) outside the documentation scope "
                    f"{ALLOWED_SCOPE_PREFIX!r}: {', '.join(outside)}",
                )
            )
        else:
            checks.append(
                Check(
                    "documentation_scope",
                    True,
                    f"all changed paths stay under {ALLOWED_SCOPE_PREFIX!r}: {', '.join(paths)}",
                )
            )
    finally:
        tree.cleanup()

    return result


# --- Evidence -------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def load_frozen_sha(evidence_dir: Path) -> str:
    path = evidence_dir / ACCEPTED_FILE
    if not path.is_file():
        return ""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    value = document.get("accepted_sha", "")
    return value if isinstance(value, str) else ""


def freeze(evidence_dir: Path, result: AcceptanceResult, candidate: str, base: str | None) -> DeployRequest:
    request = build_deploy_request(result.accepted_sha)
    record = {
        "schema": EVIDENCE_SCHEMA,
        "service": SERVICE,
        "target": TARGET,
        "repository": REPOSITORY,
        "accepted_sha": result.accepted_sha,
        "candidate_sha": candidate,
        "marker_path": MARKER_PATH,
        "marker_regex": MARKER_REGEX_POSIX,
        "marker_line": result.marker_line,
        "changed_paths": result.changed_paths,
        "allowed_scope": ALLOWED_SCOPE_PREFIX,
        "scope_base": base or "commit",
        "actor": ACTOR,
        "frozen_at": _now(),
        "gate": "docs/acceptance-gate/checks/svc-a-row18/gate.py",
        "checks": [check.as_dict() for check in result.checks],
    }
    # Freeze once: an existing record is the immutable deploy input.
    if not (evidence_dir / ACCEPTED_FILE).exists():
        _write_atomic(evidence_dir / ACCEPTED_FILE, json.dumps(record, indent=2, sort_keys=True) + "\n")
    _write_atomic(evidence_dir / REQUEST_FILE, request.to_text())
    return request


def record_rejection(evidence_dir: Path, result: AcceptanceResult, candidate: str) -> None:
    record = {
        "schema": REJECTION_SCHEMA,
        "service": SERVICE,
        "target": TARGET,
        "repository": REPOSITORY,
        "candidate_sha": candidate,
        "reason_code": result.reason_code or "ACCEPTANCE_FAILED",
        "reason": result.reason or "acceptance failed",
        "deploy_request_emitted": False,
        "recorded_at": _now(),
        "checks": [check.as_dict() for check in result.checks],
    }
    _append_jsonl(evidence_dir / REJECTION_FILE, record)


# --- Optional handoff to the deploy node ---------------------------------


def _deploy_idempotency_key(sha: str) -> str:
    material = f"{SERVICE}|{TARGET}|{sha}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def submit_deploy_request(url: str, request: DeployRequest, timeout: float = 10.0) -> dict[str, Any]:
    request.validate()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise GateError("SUBMIT_URL_INVALID", f"deploy node URL must be http(s): {url!r}")
    body = json.dumps(request.as_dict()).encode("utf-8")
    http_request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": _deploy_idempotency_key(request.accepted_sha),
        },
    )
    status = 0
    payload = b""
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            status = response.status
            payload = response.read(4096)
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read(4096)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateError("SUBMIT_FAILED", f"deploy node request failed: {exc}") from exc
    return {
        "schema": SUBMISSION_SCHEMA,
        "url": url,
        "accepted_sha": request.accepted_sha,
        "idempotency_key": _deploy_idempotency_key(request.accepted_sha),
        "status_code": status,
        "ok": 200 <= status < 300,
        "body_sha256": hashlib.sha256(payload).hexdigest(),
        "body_preview": payload.decode("utf-8", "replace")[:500],
        "submitted_at": _now(),
    }


# --- CLI ------------------------------------------------------------------


def _print_result(result: AcceptanceResult, request: DeployRequest | None, as_json: bool) -> None:
    payload: dict[str, Any] = {
        "accepted": result.accepted,
        "service": SERVICE,
        "target": TARGET,
        "accepted_sha": result.accepted_sha,
        "reason_code": result.reason_code,
        "reason": result.reason,
        "checks": [check.as_dict() for check in result.checks],
        "deploy_request": request.to_text() if request else None,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for check in result.checks:
        print(f"[{'PASS' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
    if result.accepted and request:
        print("\nACCEPTED")
        print(request.to_text(), end="")
    else:
        print(f"\nREJECTED: {result.reason}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="svc-a row #18 accepted-SHA acceptance gate")
    parser.add_argument("--repo", default=".", help="path to the svc-a git checkout (read-only)")
    parser.add_argument("--candidate", default="", help="full candidate git commit SHA")
    parser.add_argument("--base", default=None, help="optional base ref for the change-scope diff")
    parser.add_argument(
        "--evidence-dir",
        default="",
        help="external evidence area (accepted_sha.json, deployment-request.txt, rejections.jsonl)",
    )
    parser.add_argument("--submit-url", default="", help="optional deploy-node intake URL")
    parser.add_argument(
        "--validate-request",
        default="",
        help="validate an existing deploy-request file and exit",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.validate_request:
        text = Path(args.validate_request).read_text(encoding="utf-8")
        errors = validate_deploy_request(text)
        if errors:
            for error in errors:
                print(f"REJECTED: {error}", file=sys.stderr)
            return 2
        print("VALID")
        return 0

    if not args.candidate:
        parser.error("--candidate is required (or use --validate-request)")
    if not args.evidence_dir:
        parser.error("--evidence-dir is required so acceptance has an external evidence area")

    repo = Path(args.repo)
    candidate = args.candidate.strip()
    evidence_dir = Path(args.evidence_dir)

    try:
        result = evaluate(repo, candidate, args.base)
    except GateError as exc:
        print(f"REJECTED: {exc.code}: {exc}", file=sys.stderr)
        return 2

    if result.accepted:
        frozen = load_frozen_sha(evidence_dir)
        if frozen and frozen != result.accepted_sha:
            result.checks.append(
                Check(
                    "accepted_sha_frozen",
                    False,
                    f"accepted_sha is already frozen to {frozen}; refusing to replace it with {result.accepted_sha}",
                )
            )

    request: DeployRequest | None = None
    if result.accepted:
        request = freeze(evidence_dir, result, candidate, args.base)
        _print_result(result, request, args.json)
        if args.submit_url:
            try:
                submission = submit_deploy_request(args.submit_url, request)
            except GateError as exc:
                _write_atomic(
                    evidence_dir / SUBMISSION_FILE,
                    json.dumps(
                        {
                            "schema": SUBMISSION_SCHEMA,
                            "url": args.submit_url,
                            "accepted_sha": request.accepted_sha,
                            "ok": False,
                            "failure_reason": str(exc),
                            "submitted_at": _now(),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
                print(f"SUBMISSION FAILED: {exc}", file=sys.stderr)
                return 3
            _write_atomic(
                evidence_dir / SUBMISSION_FILE,
                json.dumps(submission, indent=2, sort_keys=True) + "\n",
            )
            if not submission["ok"]:
                print(
                    f"SUBMISSION REJECTED: HTTP {submission['status_code']}: {submission['body_preview']}",
                    file=sys.stderr,
                )
                return 3
        return 0

    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        record_rejection(evidence_dir, result, candidate)
    except OSError as exc:
        print(f"WARNING: could not record rejection: {exc}", file=sys.stderr)
    _print_result(result, None, args.json)
    return 2


if __name__ == "__main__":
    sys.exit(main())
