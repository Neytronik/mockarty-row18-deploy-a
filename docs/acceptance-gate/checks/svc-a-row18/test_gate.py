#!/usr/bin/env python3
"""Tests for the svc-a row #18 accepted-SHA acceptance gate.

Every test builds a disposable git repository, so the cases are exactly the
contract cases from the design section:

* pass: marker present and the accepted SHA is recorded;
* fail: missing ``docs/msdeploy.md`` -> no deploy request;
* fail: marker missing -> no deploy request and a recorded reason;
* fail: a deploy request without ``accepted_sha`` is rejected.

Run with::

    python3 -m unittest discover \
      -s docs/acceptance-gate/checks/svc-a-row18 -p 'test_*.py' -v
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate  # noqa: E402

MARKER_TEXT = """# svc-a deploy documentation

Row #18 deploy marker: row #18
Alias: row18
Service: svc-a
Target: k8s-prod-a
Repository: https://github.com/Neytronik/mockarty-row18-deploy-a.git
"""

OTHER_REPO = "https://github.com/Neytronik/mockarty-row18-deploy-b.git"


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


class RepoFixture:
    """A throwaway svc-a-shaped git repository."""

    def __init__(self, root: Path, origin: str = gate.REPOSITORY, name: str = "svc-a") -> None:
        self.path = root / name
        self.path.mkdir(parents=True)
        git(self.path, "init", "-q", "-b", "main")
        git(self.path, "config", "user.email", "gate@example.test")
        git(self.path, "config", "user.name", "gate test")
        git(self.path, "remote", "add", "origin", origin)

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message)
        return git(self.path, "rev-parse", "HEAD")

    def status(self) -> str:
        return git(self.path, "status", "--porcelain")


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="row18-gate-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.evidence = self.tmp / "evidence" / "accepted_sha" / "svc-a"
        self.repo = RepoFixture(self.tmp)

    def run_gate(self, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gate.main(
                [
                    "--repo",
                    str(self.repo.path),
                    "--evidence-dir",
                    str(self.evidence),
                    *extra,
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def acceptance_record(self) -> dict:
        return json.loads((self.evidence / gate.ACCEPTED_FILE).read_text(encoding="utf-8"))

    def rejections(self) -> list[dict]:
        path = self.evidence / gate.REJECTION_FILE
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class PassCase(GateCase):
    def test_marker_present_freezes_full_sha_and_emits_request(self) -> None:
        self.repo.write("docs/msdeploy.md", MARKER_TEXT)
        sha = self.repo.commit("docs: add row #18 marker")

        code, out, err = self.run_gate("--candidate", sha)

        self.assertEqual(code, 0, err)
        self.assertEqual(self.repo.status(), "", "the gate must not mutate the source repository")
        self.assertEqual(git(self.repo.path, "rev-parse", "HEAD"), sha)
        record = self.acceptance_record()
        self.assertEqual(record["schema"], gate.EVIDENCE_SCHEMA)
        self.assertEqual(record["service"], "svc-a")
        self.assertEqual(record["target"], "k8s-prod-a")
        self.assertEqual(record["accepted_sha"], sha)
        self.assertRegex(record["accepted_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(record["changed_paths"], ["docs/msdeploy.md"])
        self.assertEqual(record["marker_line"], "Row #18 deploy marker: row #18")

        request_text = (self.evidence / gate.REQUEST_FILE).read_text(encoding="utf-8")
        self.assertEqual(
            request_text,
            f"service=svc-a\ntarget=k8s-prod-a\naccepted_sha={sha}\nactor=deploy-node\n",
        )
        self.assertIn("ACCEPTED", out)
        self.assertEqual(gate.parse_deploy_request(request_text).accepted_sha, sha)

    def test_acceptance_is_idempotent_for_the_same_sha(self) -> None:
        self.repo.write("docs/msdeploy.md", MARKER_TEXT)
        sha = self.repo.commit("docs: add row #18 marker")

        first = self.run_gate("--candidate", sha)
        frozen_at = self.acceptance_record()["frozen_at"]
        second = self.run_gate("--candidate", sha)

        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], 0)
        record = self.acceptance_record()
        self.assertEqual(record["accepted_sha"], sha)
        self.assertEqual(record["frozen_at"], frozen_at, "an accepted SHA is frozen, not re-timestamped")
        self.assertEqual(len(self.rejections()), 0)

    def test_accepted_sha_cannot_be_replaced(self) -> None:
        self.repo.write("docs/msdeploy.md", MARKER_TEXT)
        first = self.repo.commit("docs: add row #18 marker")
        self.repo.write("docs/notes.md", "additional documentation\n")
        second = self.repo.commit("docs: add notes")

        self.assertEqual(self.run_gate("--candidate", first)[0], 0)
        code, out, _ = self.run_gate("--candidate", second)

        self.assertEqual(code, 2)
        self.assertIn("ACCEPTED_SHA_ALREADY_FROZEN", out)
        self.assertEqual(self.acceptance_record()["accepted_sha"], first)
        request = (self.evidence / gate.REQUEST_FILE).read_text(encoding="utf-8")
        self.assertIn(f"accepted_sha={first}", request)
        self.assertNotIn(second, request)


class MissingFileCase(GateCase):
    def test_missing_msdeploy_md_causes_no_deploy_request(self) -> None:
        self.repo.write("docs/other.md", "row #18 documentation\n")
        sha = self.repo.commit("docs: unrelated documentation")

        code, _, err = self.run_gate("--candidate", sha)

        self.assertEqual(code, 2)
        self.assertFalse((self.evidence / gate.REQUEST_FILE).exists())
        self.assertFalse((self.evidence / gate.ACCEPTED_FILE).exists())
        record = self.acceptance_record_safe()
        self.assertEqual(record, {})
        rejection = self.rejections()[-1]
        self.assertEqual(rejection["reason_code"], "MARKER_FILE_MISSING")
        self.assertFalse(rejection["deploy_request_emitted"])
        self.assertEqual(rejection["candidate_sha"], sha)
        self.assertIn("MARKER_FILE_MISSING", err + json.dumps(rejection))

    def acceptance_record_safe(self) -> dict:
        path = self.evidence / gate.ACCEPTED_FILE
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


class MissingMarkerCase(GateCase):
    def test_marker_missing_causes_no_request_and_records_reason(self) -> None:
        self.repo.write(
            "docs/msdeploy.md",
            "# svc-a deploy documentation\n\nService: svc-a\nTarget: k8s-prod-a\n",
        )
        sha = self.repo.commit("docs: marker-less documentation")

        code, out, _ = self.run_gate("--candidate", sha)

        self.assertEqual(code, 2)
        self.assertFalse((self.evidence / gate.REQUEST_FILE).exists())
        self.assertEqual(self.rejections()[-1]["reason_code"], "MARKER_MISSING")
        self.assertEqual(self.rejections()[-1]["deploy_request_emitted"], False)
        self.assertIn("MARKER_MISSING", out)

    def test_service_and_target_must_be_named(self) -> None:
        self.repo.write("docs/msdeploy.md", "row #18\nService: other\nTarget: other\n")
        sha = self.repo.commit("docs: wrong service")

        code, out, _ = self.run_gate("--candidate", sha)

        self.assertEqual(code, 2)
        self.assertFalse((self.evidence / gate.REQUEST_FILE).exists())
        codes = {record["reason_code"] for record in self.rejections()}
        self.assertEqual(codes, {"SERVICE_MARKER_MISSING"})


class DeployRequestCase(unittest.TestCase):
    def test_request_without_accepted_sha_is_rejected(self) -> None:
        text = "service=svc-a\ntarget=k8s-prod-a\nactor=deploy-node\n"

        errors = gate.validate_deploy_request(text)

        self.assertTrue(any("accepted_sha is required" in error for error in errors), errors)
        with self.assertRaises(ValueError):
            gate.parse_deploy_request(text)
        with self.assertRaises(ValueError):
            gate.build_deploy_request("")

    def test_request_with_short_or_foreign_sha_is_rejected(self) -> None:
        short = "service=svc-a\ntarget=k8s-prod-a\naccepted_sha=5c459c0\nactor=deploy-node\n"
        foreign = (
            "service=svc-a\ntarget=k8s-prod-a\n"
            f"accepted_sha={'0' * 39}\nactor=deploy-node\n"
        )

        self.assertTrue(any("full 40-character" in error for error in gate.validate_deploy_request(short)))
        self.assertTrue(any("full 40-character" in error for error in gate.validate_deploy_request(foreign)))

    def test_wrong_service_target_or_actor_is_rejected(self) -> None:
        text = (
            "service=svc-b\ntarget=k8s-prod-b\n"
            "accepted_sha=5c459c09278961dfe6cbf5729ee318c859dda71e\nactor=operator\n"
        )

        errors = gate.validate_deploy_request(text)

        self.assertTrue(any("service must be 'svc-a'" in error for error in errors), errors)
        self.assertTrue(any("target must be 'k8s-prod-a'" in error for error in errors), errors)
        self.assertTrue(any("actor must be 'deploy-node'" in error for error in errors), errors)

    def test_valid_request_round_trips(self) -> None:
        request = gate.build_deploy_request("a" * 40)

        self.assertEqual(gate.parse_deploy_request(request.to_text()), request)
        self.assertEqual(gate.validate_deploy_request(request.as_dict()), [])

    def test_validate_request_cli_rejects_missing_accepted_sha(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "deployment-request.txt"
            path.write_text("service=svc-a\ntarget=k8s-prod-a\nactor=deploy-node\n", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = gate.main(["--validate-request", str(path)])

        self.assertEqual(code, 2)
        self.assertIn("accepted_sha is required", err.getvalue())


class ScopeCase(GateCase):
    def test_change_outside_documentation_scope_is_rejected(self) -> None:
        self.repo.write("docs/msdeploy.md", MARKER_TEXT)
        self.repo.commit("docs: add row #18 marker")
        self.repo.write("README.md", "# svc-a\n")
        sha = self.repo.commit("docs: also change the readme")

        code, out, _ = self.run_gate("--candidate", sha)

        self.assertEqual(code, 2)
        self.assertIn("OUT_OF_SCOPE_CHANGE", out)
        self.assertFalse((self.evidence / gate.REQUEST_FILE).exists())
        rejection = self.rejections()[-1]
        self.assertIn("README.md", rejection["reason"])

    def test_repository_must_be_the_svc_a_repository(self) -> None:
        other = RepoFixture(self.tmp, origin=OTHER_REPO, name="svc-b")
        other.write("docs/msdeploy.md", MARKER_TEXT)
        sha = other.commit("docs: marker")

        code, out, _ = self.run_gate_with_repo(other.path, sha)

        self.assertEqual(code, 2)
        self.assertIn("REPOSITORY_MISMATCH", out)
        self.assertFalse((self.evidence / gate.REQUEST_FILE).exists())

    def test_short_candidate_is_rejected(self) -> None:
        self.repo.write("docs/msdeploy.md", MARKER_TEXT)
        sha = self.repo.commit("docs: add row #18 marker")

        code, out, _ = self.run_gate("--candidate", sha[:7])

        self.assertEqual(code, 2)
        self.assertIn("CANDIDATE_SHA_NOT_FULL", out)
        self.assertFalse((self.evidence / gate.REQUEST_FILE).exists())

    def run_gate_with_repo(self, repo: Path, sha: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gate.main(
                ["--repo", str(repo), "--evidence-dir", str(self.evidence), "--candidate", sha]
            )
        return code, out.getvalue(), err.getvalue()


class _Recorder(BaseHTTPRequestHandler):
    received: list[dict] = []
    response_status = 202

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).received.append(
            {"headers": {key.lower(): value for key, value in self.headers.items()}, "body": body}
        )
        payload = b'{"status":"accepted","deployment_id":"dep-test"}'
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        return


class HandoffCase(GateCase):
    def setUp(self) -> None:
        super().setUp()
        _Recorder.received = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/deployments"

    def test_accepted_request_is_handed_to_the_deploy_node(self) -> None:
        self.repo.write("docs/msdeploy.md", MARKER_TEXT)
        sha = self.repo.commit("docs: add row #18 marker")

        code, _, err = self.run_gate("--candidate", sha, "--submit-url", self.url)

        self.assertEqual(code, 0, err)
        self.assertEqual(len(_Recorder.received), 1)
        sent = json.loads(_Recorder.received[0]["body"])
        self.assertEqual(sent, {
            "service": "svc-a",
            "target": "k8s-prod-a",
            "accepted_sha": sha,
            "actor": "deploy-node",
        })
        self.assertTrue(_Recorder.received[0]["headers"]["idempotency-key"].startswith("sha256:"))
        submission = json.loads((self.evidence / gate.SUBMISSION_FILE).read_text(encoding="utf-8"))
        self.assertTrue(submission["ok"])
        self.assertEqual(submission["status_code"], 202)

    def test_rejected_acceptance_never_calls_the_deploy_node(self) -> None:
        self.repo.write("docs/msdeploy.md", "no marker here\n")
        sha = self.repo.commit("docs: marker-less")

        code, _, _ = self.run_gate("--candidate", sha, "--submit-url", self.url)

        self.assertEqual(code, 2)
        self.assertEqual(_Recorder.received, [])
        self.assertFalse((self.evidence / gate.REQUEST_FILE).exists())


class ReadOnlyCase(GateCase):
    def test_gate_does_not_create_worktrees_or_touch_repository_metadata(self) -> None:
        self.repo.write("docs/msdeploy.md", MARKER_TEXT)
        sha = self.repo.commit("docs: add row #18 marker")
        before = sorted(
            str(path.relative_to(self.repo.path))
            for path in (self.repo.path / ".git").rglob("*")
            if path.is_file()
        )

        self.assertEqual(self.run_gate("--candidate", sha)[0], 0)

        after = sorted(
            str(path.relative_to(self.repo.path))
            for path in (self.repo.path / ".git").rglob("*")
            if path.is_file()
        )
        self.assertEqual(before, after)
        self.assertEqual(self.repo.status(), "")
        self.assertFalse((self.repo.path / "acceptance-gate").exists())
        self.assertFalse((self.repo.path / "evidence").exists())


if __name__ == "__main__":
    unittest.main()
