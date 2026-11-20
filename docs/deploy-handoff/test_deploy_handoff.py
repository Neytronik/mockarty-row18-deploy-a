"""Tests for the row #18 variant B svc-a deployment handoff contract.

Run with::

    python3 -m unittest discover -s docs/deploy-handoff -p 'test_*.py' -v
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deploy_handoff as handoff  # noqa: E402

SHA = "85f3c87bfe98410461cccdf69fd629b8535e90fa"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"
DEPLOYER_IDENTITY = "mockarty-kubernetes-deployer/row18-svc-a-variantB@k8s-live-a"
FOREIGN = "https://github.com/Neytronik/mockarty-row18-deploy-b.git"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class RecordingOpener:
    """Answers queued responses and records every outgoing request."""

    def __init__(self, responses: List[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float = 15.0) -> FakeResponse:
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": dict(request.header_items()),
                "body": json.loads(request.data.decode("utf-8")) if request.data else None,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected HTTP call: " + request.full_url)
        return self.responses.pop(0)


def accepted_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "accepted": True,
        "deploymentId": "deploy-row18-svc-a-variantB",
        "commitSha": SHA,
        "target": handoff.TARGET,
        "environment": handoff.ENVIRONMENT,
        "repository": handoff.REPOSITORY,
        "deployerIdentity": DEPLOYER_IDENTITY,
        "manualDeploy": False,
    }
    payload.update(overrides)
    return payload


def health_window(**overrides: Any) -> Dict[str, Any]:
    window = {
        "start": "2026-11-20T20:10:00Z",
        "end": "2026-11-20T20:10:20Z",
        "durationSeconds": 20.0,
        "intervalSeconds": 10,
        "probesRequested": 3,
        "probesAttempted": 3,
        "consecutiveSuccessfulProbes": 3,
        "observedStatuses": [200, 200, 200],
        "failures": [],
        "green": True,
    }
    window.update(overrides)
    return window


def green_receipt(**overrides: Any) -> Dict[str, Any]:
    request = handoff.build_deploy_request(SHA)
    receipt = handoff.build_receipt(
        request,
        accepted_payload(),
        health_window(),
    )
    receipt.update(overrides)
    return receipt


class GatedOpener(RecordingOpener):
    def __call__(self, request: Any, timeout: float = 15.0) -> FakeResponse:
        super().__call__(request, timeout)
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# DeployRequest
# ---------------------------------------------------------------------------


class DeployRequestTests(unittest.TestCase):
    def test_builds_the_contract_request(self) -> None:
        request = handoff.build_deploy_request(SHA)
        self.assertEqual(
            request.to_payload(),
            {
                "service": "svc-a",
                "repository": "https://github.com/Neytronik/mockarty-row18-deploy-a.git",
                "commitSha": SHA,
                "target": "k8s-live-a",
                "environment": "stage",
                "deployer": "mockarty-kubernetes-deployer",
            },
        )

    def test_idempotency_key_is_derived_from_service_sha_and_target(self) -> None:
        first = handoff.build_deploy_request(SHA).idempotency_key()
        second = handoff.build_deploy_request(SHA).idempotency_key()
        different = handoff.build_deploy_request(OTHER_SHA).idempotency_key()
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertTrue(first.startswith("sha256:"))

    def test_rejects_missing_sha(self) -> None:
        for value in ("", None):
            with self.subTest(value=value):
                with self.assertRaises(handoff.RequestError):
                    handoff.build_deploy_request(value)  # type: ignore[arg-type]

    def test_rejects_non_40_character_sha(self) -> None:
        for value in (SHA[:7], SHA + "0", SHA[:-1], SHA.upper(), "z" * 40, SHA[:39] + "g"):
            with self.subTest(value=value):
                with self.assertRaises(handoff.RequestError):
                    handoff.build_deploy_request(value)

    def test_rejects_sha_that_is_not_the_delivered_head(self) -> None:
        with self.assertRaises(handoff.RequestError):
            handoff.assert_delivered_sha(OTHER_SHA, SHA)
        handoff.assert_delivered_sha(SHA, SHA)  # no raise

    def test_rejects_out_of_scope_service_repository_target_environment_deployer(self) -> None:
        cases = {
            "service": {"service": "svc-b"},
            "repository": {"repository": FOREIGN},
            "target": {"target": "k8s-live-b"},
            "environment": {"environment": "prod"},
            "deployer": {"deployer": "human-operator"},
        }
        for name, overrides in cases.items():
            with self.subTest(field=name):
                with self.assertRaises(handoff.RequestError):
                    handoff.build_deploy_request(SHA, **overrides)

    def test_mockarty_attribution_rules(self) -> None:
        self.assertTrue(handoff.is_mockarty_attributable("mockarty-kubernetes-deployer"))
        self.assertTrue(handoff.is_mockarty_attributable("Mockarty deploy node / svc-a"))
        for identity in ("", None, "deploy-bot", "manual-kubectl", "mockarty-human", "mockarty-ssh-runner"):
            with self.subTest(identity=identity):
                self.assertFalse(handoff.is_mockarty_attributable(identity))


# ---------------------------------------------------------------------------
# Repository scope (R1/R2) — real temporary git repository
# ---------------------------------------------------------------------------


def init_repository(root: str, files: Dict[str, str], origin: str = handoff.REPOSITORY) -> str:
    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(["git", "-C", root, "remote", "add", "origin", origin], check=True)
    commit(root, files, "chore: base")
    return subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(root: str, files: Dict[str, str], message: str) -> None:
    for relative, content in files.items():
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            root,
            "-c",
            "user.name=Test Agent",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
    )


class RepositoryScopeTests(unittest.TestCase):
    def test_docs_only_commit_with_svc_a_origin_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = init_repository(root, {"README.md": "fixture\n", "deploy/app.yaml": "kind: Deployment\n"})
            commit(
                root,
                {
                    "docs/msdeploy.md": "# marker\n\nk8s-deploy-variant-B-ok\n",
                    "docs/deploy-handoff/README.md": "handoff\n",
                },
                "docs: marker",
            )
            report = handoff.check_repository_scope(root, base)
            self.assertEqual(report["problems"], [])
            self.assertTrue(report["docsOnly"])
            self.assertEqual(
                sorted(report["changedPaths"]),
                ["docs/deploy-handoff/README.md", "docs/msdeploy.md"],
            )

    def test_non_docs_change_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = init_repository(root, {"README.md": "fixture\n"})
            commit(root, {"README.md": "fixture\n", "deploy/app.yaml": "kind: Deployment\n"}, "chore: manifest")
            report = handoff.check_repository_scope(root, base)
            self.assertFalse(report["docsOnly"])
            self.assertTrue(any("outside docs/" in problem for problem in report["problems"]))

    def test_foreign_origin_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            init_repository(root, {"docs/msdeploy.md": "marker\n"}, origin=FOREIGN)
            report = handoff.check_repository_scope(root)
            self.assertTrue(any("svc-a repository" in problem for problem in report["problems"]))

    def test_scope_check_runs_git_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            init_repository(root, {"docs/msdeploy.md": "marker\n"})
            recorded: List[List[str]] = []
            real_run = subprocess.run

            def spy(argv: List[str], *args: Any, **kwargs: Any) -> Any:
                recorded.append(list(argv))
                return real_run(argv, *args, **kwargs)

            with mock.patch.object(handoff.subprocess, "run", side_effect=spy):
                handoff.check_repository_scope(root)
            self.assertTrue(recorded)
            for argv in recorded:
                self.assertEqual(argv[0], "git")


# ---------------------------------------------------------------------------
# Deploy node
# ---------------------------------------------------------------------------


class DeployNodeTests(unittest.TestCase):
    ENDPOINT = "http://127.0.0.1:5970/stubs/sandbox/row18/svc-a/variantB/deployments"

    def submit(self, response: FakeResponse, **request_overrides: Any) -> Dict[str, Any]:
        opener = RecordingOpener([response])
        request = handoff.build_deploy_request(SHA, **request_overrides)
        return handoff.submit_deploy_request(request, self.ENDPOINT, opener=opener)

    def test_posts_the_exact_commit_with_an_idempotency_key(self) -> None:
        opener = RecordingOpener([FakeResponse(202, accepted_payload())])
        request = handoff.build_deploy_request(SHA)
        result = handoff.submit_deploy_request(request, self.ENDPOINT, opener=opener)
        self.assertEqual(result["deployerIdentity"], DEPLOYER_IDENTITY)
        call = opener.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["body"], request.to_payload())
        headers = {key.lower(): value for key, value in call["headers"].items()}
        self.assertEqual(headers["idempotency-key"], request.idempotency_key())

    def test_refuses_non_2xx(self) -> None:
        with self.assertRaises(handoff.DeployRefused):
            self.submit(FakeResponse(422, {"accepted": False, "reason": "manual deployment"}))

    def test_refuses_unaccepted_answer(self) -> None:
        with self.assertRaises(handoff.DeployRefused):
            self.submit(FakeResponse(202, accepted_payload(accepted=False)))

    def test_refuses_a_different_commit(self) -> None:
        with self.assertRaises(handoff.DeployRefused):
            self.submit(FakeResponse(202, accepted_payload(commitSha=OTHER_SHA)))

    def test_refuses_a_different_target_or_environment(self) -> None:
        for overrides in ({"target": "k8s-live-b"}, {"environment": "prod"}):
            with self.subTest(**overrides):
                with self.assertRaises(handoff.DeployRefused):
                    self.submit(FakeResponse(202, accepted_payload(**overrides)))

    def test_refuses_manual_deploy_flag(self) -> None:
        with self.assertRaises(handoff.DeployRefused):
            self.submit(FakeResponse(202, accepted_payload(manualDeploy=True)))

    def test_refuses_a_non_mockarty_actor(self) -> None:
        for identity in ("human-operator", "okta:deploy-bot", "kubectl@laptop", None):
            with self.subTest(identity=identity):
                with self.assertRaises(handoff.DeployRefused):
                    self.submit(FakeResponse(202, accepted_payload(deployerIdentity=identity, actor=identity)))


# ---------------------------------------------------------------------------
# Health window
# ---------------------------------------------------------------------------


class HealthWindowTests(unittest.TestCase):
    def test_three_consecutive_200s_are_green_at_ten_second_intervals(self) -> None:
        observed: List[float] = []
        now = {"value": 1000.0}

        def sleep(seconds: float) -> None:
            observed.append(seconds)
            now["value"] += seconds

        window = handoff.run_health_window(
            probe_fn=lambda url: 200,
            sleep=sleep,
            clock=lambda: now["value"],
        )
        self.assertTrue(window["green"])
        self.assertEqual(window["consecutiveSuccessfulProbes"], 3)
        self.assertEqual(window["observedStatuses"], [200, 200, 200])
        self.assertEqual(observed, [10, 10])
        self.assertEqual(window["intervalSeconds"], 10)
        self.assertEqual(window["durationSeconds"], 20.0)
        self.assertEqual(window["failures"], [])

    def test_a_non_200_probe_makes_the_window_red(self) -> None:
        statuses = iter([200, 503, 200])
        window = handoff.run_health_window(probe_fn=lambda url: next(statuses), sleep=lambda _: None)
        self.assertFalse(window["green"])
        self.assertLess(window["consecutiveSuccessfulProbes"], 3)
        self.assertTrue(window["failures"])
        self.assertIn("503", window["failures"][0])

    def test_an_intermittent_transport_failure_makes_the_window_red(self) -> None:
        calls = {"count": 0}

        def flaky(url: str) -> int:
            calls["count"] += 1
            if calls["count"] == 2:
                raise handoff.HealthWindowError("connection reset")
            return 200

        window = handoff.run_health_window(probe_fn=flaky, sleep=lambda _: None)
        self.assertFalse(window["green"])
        self.assertTrue(any("connection reset" in failure for failure in window["failures"]))

    def test_a_window_shorter_than_three_probes_is_refused(self) -> None:
        with self.assertRaises(handoff.HealthWindowError):
            handoff.run_health_window(probes=2, probe_fn=lambda url: 200, sleep=lambda _: None)

    def test_probe_url_and_method_are_the_acceptance_surface(self) -> None:
        seen: List[str] = []

        def probe(url: str) -> int:
            seen.append(url)
            return 200

        handoff.run_health_window(probe_fn=probe, sleep=lambda _: None)
        self.assertEqual(seen, [handoff.ACCEPTANCE_URL] * 3)
        self.assertEqual(handoff.HEALTH_METHOD, "GET")


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


class ReceiptTests(unittest.TestCase):
    def test_green_receipt_binds_the_delivered_commit(self) -> None:
        receipt = green_receipt()
        handoff.validate_receipt(receipt, SHA)
        self.assertEqual(receipt["service"], "svc-a")
        self.assertEqual(receipt["target"], "k8s-live-a")
        self.assertEqual(receipt["environment"], "stage")
        self.assertEqual(receipt["healthProbe"]["observedStatus"], 200)
        self.assertEqual(receipt["status"], "green")

    def test_missing_top_level_field_fails(self) -> None:
        for field_name in handoff.RECEIPT_REQUIRED_FIELDS:
            with self.subTest(field=field_name):
                receipt = green_receipt()
                del receipt[field_name]
                with self.assertRaises(handoff.ReceiptError):
                    handoff.validate_receipt(receipt, SHA)

    def test_missing_health_window_field_fails(self) -> None:
        for field_name in handoff.HEALTH_WINDOW_REQUIRED_FIELDS:
            with self.subTest(field=field_name):
                receipt = green_receipt()
                del receipt["healthWindow"][field_name]
                with self.assertRaises(handoff.ReceiptError):
                    handoff.validate_receipt(receipt, SHA)

    def test_missing_health_probe_field_fails(self) -> None:
        for field_name in handoff.HEALTH_PROBE_REQUIRED_FIELDS:
            with self.subTest(field=field_name):
                receipt = green_receipt()
                del receipt["healthProbe"][field_name]
                with self.assertRaises(handoff.ReceiptError):
                    handoff.validate_receipt(receipt, SHA)

    def test_receipt_sha_must_equal_delivered_sha(self) -> None:
        receipt = green_receipt(commitSha=OTHER_SHA)
        with self.assertRaises(handoff.ReceiptError):
            handoff.validate_receipt(receipt, SHA)

    def test_receipt_sha_must_be_a_full_sha(self) -> None:
        receipt = green_receipt(commitSha=SHA[:7])
        with self.assertRaises(handoff.ReceiptError):
            handoff.validate_receipt(receipt, SHA)

    def test_receipt_target_and_environment_must_match(self) -> None:
        for overrides in ({"target": "k8s-live-b"}, {"environment": "prod"}, {"service": "svc-b"}):
            with self.subTest(**overrides):
                with self.assertRaises(handoff.ReceiptError):
                    handoff.validate_receipt(green_receipt(**overrides), SHA)

    def test_receipt_with_a_manual_deployer_identity_fails(self) -> None:
        for identity in ("manual-kubectl", "human@ops", "deploy-bot"):
            with self.subTest(identity=identity):
                with self.assertRaises(handoff.ReceiptError):
                    handoff.validate_receipt(green_receipt(deployerIdentity=identity), SHA)

    def test_receipt_with_a_non_200_probe_fails(self) -> None:
        receipt = green_receipt()
        receipt["healthProbe"]["observedStatus"] = 503
        with self.assertRaises(handoff.ReceiptError):
            handoff.validate_receipt(receipt, SHA)

    def test_receipt_with_a_short_or_red_window_fails(self) -> None:
        cases = [
            {"consecutiveSuccessfulProbes": 2},
            {"durationSeconds": 0},
            {"green": False},
            {"observedStatuses": [200, 500, 200]},
            {"failures": ["probe 2 raised URLError"]},
        ]
        for overrides in cases:
            with self.subTest(**overrides):
                receipt = green_receipt()
                receipt["healthWindow"].update(overrides)
                with self.assertRaises(handoff.ReceiptError):
                    handoff.validate_receipt(receipt, SHA)

    def test_receipt_status_must_be_green(self) -> None:
        with self.assertRaises(handoff.ReceiptError):
            handoff.validate_receipt(green_receipt(status="red"), SHA)

    def test_receipt_with_foreign_repository_evidence_fails(self) -> None:
        for overrides in ({"repository": FOREIGN}, {"deploymentId": "deploy-svc-b"}):
            with self.subTest(**overrides):
                with self.assertRaises(handoff.HandoffError):
                    handoff.validate_receipt(green_receipt(**overrides), SHA)

    def test_red_window_produces_a_red_receipt(self) -> None:
        request = handoff.build_deploy_request(SHA)
        receipt = handoff.build_receipt(
            request,
            accepted_payload(),
            health_window(green=False, observedStatuses=[200, 503], consecutiveSuccessfulProbes=1),
        )
        self.assertEqual(receipt["status"], "red")
        with self.assertRaises(handoff.ReceiptError):
            handoff.validate_receipt(receipt, SHA)


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


class VerifyFlowTests(unittest.TestCase):
    DEPLOY = "http://127.0.0.1:5970/stubs/sandbox/row18/svc-a/variantB/deployments"
    STORE = "http://127.0.0.1:5970/stubs/sandbox/row18/svc-a/variantB/receipts"

    def run_verify(self, opener: RecordingOpener, probe_fn: Any) -> Dict[str, Any]:
        now = {"value": 1000.0}

        def sleep(seconds: float) -> None:
            now["value"] += seconds

        return handoff.verify(
            delivered_sha=SHA,
            deploy_endpoint=self.DEPLOY,
            receipt_store_endpoint=self.STORE,
            probe_fn=probe_fn,
            sleep=sleep,
            clock=lambda: now["value"],
            opener=opener,
        )

    def test_green_flow_posts_the_receipt_after_the_health_window(self) -> None:
        opener = RecordingOpener(
            [
                FakeResponse(202, accepted_payload()),
                FakeResponse(201, {"stored": True, "receiptId": "rcpt-row18-svc-a-variantB"}),
            ]
        )
        receipt = self.run_verify(opener, lambda url: 200)
        self.assertEqual(receipt["status"], "green")
        self.assertEqual(receipt["commitSha"], SHA)
        self.assertEqual([call["url"] for call in opener.calls], [self.DEPLOY, self.STORE])
        self.assertEqual(opener.calls[1]["body"]["commitSha"], SHA)
        self.assertEqual(receipt["evidenceStore"]["receiptId"], "rcpt-row18-svc-a-variantB")

    def test_red_health_window_writes_no_receipt(self) -> None:
        opener = RecordingOpener([FakeResponse(202, accepted_payload())])
        statuses = iter([200, 200, 500])
        with self.assertRaises(handoff.HealthWindowError):
            self.run_verify(opener, lambda url: next(statuses))
        self.assertEqual([call["url"] for call in opener.calls], [self.DEPLOY])

    def test_refused_deployment_runs_no_probe_and_writes_no_receipt(self) -> None:
        opener = RecordingOpener([FakeResponse(422, {"accepted": False, "reason": "manual actor"})])
        probes: List[str] = []
        with self.assertRaises(handoff.DeployRefused):
            self.run_verify(opener, lambda url: probes.append(url) or 200)
        self.assertEqual(probes, [])
        self.assertEqual(len(opener.calls), 1)

    def test_unconfirmed_receipt_store_is_an_error(self) -> None:
        opener = RecordingOpener(
            [FakeResponse(202, accepted_payload()), FakeResponse(201, {"stored": False})]
        )
        with self.assertRaises(handoff.ReceiptError):
            self.run_verify(opener, lambda url: 200)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def test_dry_run_prints_the_contract_request(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            init_repository(
                root,
                {"README.md": "fixture\n", "docs/msdeploy.md": "# marker\n\nk8s-deploy-variant-B-ok\n"},
            )
            out = _run_main(["--repo-dir", root, "--dry-run"])
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["request"]["service"], "svc-a")
        self.assertEqual(out["request"]["commitSha"], out["scope"]["head"])
        self.assertEqual(out["scope"]["changedPaths"], [])

    def test_dry_run_rejects_a_foreign_origin(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            init_repository(root, {"docs/msdeploy.md": "marker\n"}, origin=FOREIGN)
            out = _run_main(["--repo-dir", root, "--dry-run"])
        self.assertEqual(out["ok"], False)
        self.assertIn("repository scope violation", out["error"])

    def test_cli_rejects_a_sha_that_is_not_head(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            init_repository(root, {"docs/msdeploy.md": "marker\n"})
            out = _run_main(["--repo-dir", root, "--dry-run", "--commit-sha", OTHER_SHA])
        self.assertEqual(out["ok"], False)
        self.assertIn("does not match delivered commit", out["error"])

    def test_cli_rejects_a_base_that_is_not_a_full_sha(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            init_repository(root, {"docs/msdeploy.md": "marker\n"})
            out = _run_main(["--repo-dir", root, "--dry-run", "--base", "a84f447"])
        self.assertEqual(out["ok"], False)
        self.assertEqual(out["exitCode"], handoff.EXIT_REQUEST_INVALID)
        self.assertIn("repository scope check failed", out["error"])


def _run_main(argv: List[str]) -> Dict[str, Any]:
    buffer: List[str] = []
    with mock.patch("builtins.print", side_effect=lambda *a, **k: buffer.append(a[0] if a else "")):
        code = handoff._main(argv)
    payload = json.loads("\n".join(buffer))
    payload["exitCode"] = code
    return payload


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
