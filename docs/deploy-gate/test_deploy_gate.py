"""Tests for the row #18 deployment request contract and allowlist gate.

Run with:  python3 -m unittest discover -s docs/deploy-gate -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deploy_gate as gate  # noqa: E402

SHA = "f91910dfcf6c238763596251487c07ac41c9e378"
RUN_ID = "row18-run-svc-a"


def manifest(**overrides):
    data = {
        "runId": RUN_ID,
        "service": gate.ALLOWED_SERVICE,
        "repoUrl": gate.ALLOWED_REPO_URL,
        "branch": gate.ALLOWED_BRANCH,
        "commitSha": SHA,
        "target": gate.ALLOWED_TARGET,
        "cluster": gate.ALLOWED_CLUSTER,
        "namespace": gate.ALLOWED_NAMESPACE,
        "healthWindowSeconds": 300,
        "status": "verified",
    }
    data.update(overrides)
    return gate.RunManifest.from_dict(data)


class RecordingTransport:
    """Deterministic in-memory deploy node; records every call."""

    def __init__(self, responses, raise_timeout_on=()):
        self.responses = list(responses)
        self.raise_timeout_on = set(raise_timeout_on)
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": dict(payload), "timeout": timeout})
        index = len(self.calls)
        if index in self.raise_timeout_on:
            raise gate.TransportError("timed out")
        return self.responses.pop(0)

    @property
    def call_count(self):
        return len(self.calls)


def client(transport, **kwargs):
    kwargs.setdefault("max_attempts", 3)
    kwargs.setdefault("backoff_seconds", 0)
    kwargs.setdefault("sleep", lambda _seconds: None)
    return gate.DeployClient("http://deploy-node.test/deployments", transport=transport, **kwargs)


class AllowlistAcceptTest(unittest.TestCase):
    def test_valid_svc_a_request_is_accepted(self):
        transport = RecordingTransport(
            [(201, {"runId": "deploy-run-1", "status": "accepted", "message": "ok"})]
        )
        result = gate.submit_manifest(manifest(), client(transport))

        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.run_id, "deploy-run-1")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(transport.call_count, 1)
        payload = transport.calls[0]["payload"]
        self.assertEqual(payload["service"], "svc-a")
        self.assertEqual(payload["target"], "k8s-live-a")
        self.assertEqual(payload["namespace"], "mockarty-deploy-live")
        self.assertEqual(payload["cluster"], "Robolance stage")
        self.assertEqual(payload["commitSha"], SHA)
        self.assertEqual(payload["healthWindowSeconds"], 300)
        self.assertEqual(
            payload["idempotencyKey"],
            gate.idempotency_key(RUN_ID, SHA, "k8s-live-a"),
        )

    def test_idempotency_key_is_deterministic_and_scoped(self):
        first = gate.idempotency_key(RUN_ID, SHA, "k8s-live-a")
        second = gate.idempotency_key(RUN_ID, SHA, "k8s-live-a")
        self.assertEqual(first, second)
        self.assertNotEqual(first, gate.idempotency_key(RUN_ID + "x", SHA, "k8s-live-a"))
        self.assertNotEqual(first, gate.idempotency_key(RUN_ID, "a" * 40, "k8s-live-a"))
        self.assertNotEqual(first, gate.idempotency_key(RUN_ID, SHA, "k8s-live-b"))


class AllowlistRejectTest(unittest.TestCase):
    def assert_rejected_without_network(self, bad_manifest):
        transport = RecordingTransport([])
        with self.assertRaises(gate.GateError):
            gate.submit_manifest(bad_manifest, client(transport))
        self.assertEqual(transport.call_count, 0, "allowlist mismatch must not call /deployments")

    def test_svc_b_service_is_rejected(self):
        self.assert_rejected_without_network(manifest(service="svc-b"))

    def test_k8s_live_b_target_is_rejected(self):
        self.assert_rejected_without_network(manifest(target="k8s-live-b"))

    def test_wrong_repository_is_rejected(self):
        self.assert_rejected_without_network(
            manifest(repoUrl="https://github.com/Neytronik/mockarty-row18-deploy-b.git")
        )

    def test_wrong_branch_is_rejected(self):
        self.assert_rejected_without_network(manifest(branch="main"))

    def test_wrong_namespace_is_rejected(self):
        self.assert_rejected_without_network(manifest(namespace="mockarty-deploy-other"))

    def test_wrong_cluster_is_rejected(self):
        self.assert_rejected_without_network(manifest(cluster="Robolance prod"))

    def test_unverified_manifest_is_rejected(self):
        self.assert_rejected_without_network(manifest(status="pushed"))

    def test_missing_commit_sha_is_rejected(self):
        self.assert_rejected_without_network(manifest(commitSha=""))

    def test_short_commit_sha_is_rejected(self):
        self.assert_rejected_without_network(manifest(commitSha="f91910d"))

    def test_mismatched_commit_sha_is_rejected(self):
        transport = RecordingTransport([])
        with self.assertRaises(gate.ManifestError):
            gate.submit_manifest(manifest(), client(transport), commit_sha="a" * 40)
        self.assertEqual(transport.call_count, 0)

    def test_non_positive_health_window_is_rejected(self):
        self.assert_rejected_without_network(manifest(healthWindowSeconds=0))

    def test_rejected_request_keeps_the_deploy_node_violation(self):
        request = gate.build_request(manifest())
        bad = gate.DeploymentRequest(
            service="svc-b",
            repo_url=request.repo_url,
            branch=request.branch,
            commit_sha=request.commit_sha,
            target=request.target,
            cluster=request.cluster,
            namespace=request.namespace,
            health_window_seconds=request.health_window_seconds,
            idempotency_key=request.idempotency_key,
        )
        with self.assertRaises(gate.AllowlistError) as ctx:
            client(RecordingTransport([])).submit(bad)
        self.assertIn("service", str(ctx.exception))
        self.assertIn("svc-b", str(ctx.exception))


class IdempotencyTest(unittest.TestCase):
    def test_duplicate_post_returns_the_same_run_id(self):
        request = gate.build_request(manifest())
        key = request.idempotency_key
        # Real deploy node: first POST accepts, second POST returns duplicate.
        state = {"run_id": None}

        def transport(url, payload, timeout):
            self.assertEqual(payload["idempotencyKey"], key)
            if state["run_id"] is None:
                state["run_id"] = "deploy-row18-svc-a-abc123"
                return 201, {"runId": state["run_id"], "status": "accepted", "message": "new"}
            return 200, {"runId": state["run_id"], "status": "duplicate", "message": "existing"}

        deploy_client = client(transport)
        first = deploy_client.submit(request)
        second = deploy_client.submit(request)

        self.assertEqual(first.status, "accepted")
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(first.run_id, second.run_id)

    def test_retry_after_5xx_reuses_the_same_idempotency_key(self):
        transport = RecordingTransport(
            [
                (503, {"runId": "", "status": "error", "message": "unavailable"}),
                (500, {"runId": "", "status": "error", "message": "boom"}),
                (201, {"runId": "deploy-run-retry", "status": "accepted", "message": "ok"}),
            ]
        )
        result = gate.submit_manifest(manifest(), client(transport))

        self.assertEqual(result.run_id, "deploy-run-retry")
        self.assertEqual(result.attempts, 3)
        keys = {call["payload"]["idempotencyKey"] for call in transport.calls}
        self.assertEqual(len(keys), 1, "retries must reuse one idempotency key")

    def test_retry_after_timeout_reuses_the_same_idempotency_key(self):
        transport = RecordingTransport(
            [(201, {"runId": "deploy-run-timeout", "status": "accepted", "message": "ok"})],
            raise_timeout_on={1},
        )
        result = gate.submit_manifest(manifest(), client(transport))
        self.assertEqual(result.attempts, 2)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(
            transport.calls[0]["payload"]["idempotencyKey"],
            transport.calls[1]["payload"]["idempotencyKey"],
        )


class FailureHandlingTest(unittest.TestCase):
    def test_4xx_is_surfaced_and_not_retried(self):
        transport = RecordingTransport(
            [(422, {"runId": "", "status": "rejected", "message": "bad health window"})]
        )
        with self.assertRaises(gate.DeployRejected) as ctx:
            gate.submit_manifest(manifest(), client(transport))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(transport.call_count, 1, "4xx must not be retried")

    def test_retry_exhaustion_is_reported(self):
        transport = RecordingTransport(
            [(503, {"message": "down"}), (503, {"message": "down"}), (503, {"message": "down"})]
        )
        with self.assertRaises(gate.DeployRetryExhausted) as ctx:
            gate.submit_manifest(manifest(), client(transport))
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(transport.call_count, 3)

    def test_timeout_exhaustion_is_reported(self):
        transport = RecordingTransport([], raise_timeout_on={1, 2, 3})
        with self.assertRaises(gate.DeployRetryExhausted):
            gate.submit_manifest(manifest(), client(transport))
        self.assertEqual(transport.call_count, 3)

    def test_response_without_run_id_is_an_error(self):
        transport = RecordingTransport([(200, {"status": "accepted", "message": "ok"})])
        with self.assertRaises(gate.GateError):
            gate.submit_manifest(manifest(), client(transport))


class NoManualDeploymentTest(unittest.TestCase):
    """The orchestrator path must not contain any direct cluster command."""

    FORBIDDEN = (
        "kubectl",
        "helm",
        "subprocess",
        "os.system",
        "os.popen",
        "docker exec",
    )

    def test_orchestrator_has_no_manual_deployment_path(self):
        source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_gate.py")
        with open(source_path, "r", encoding="utf-8") as handle:
            source = handle.read().lower()
        for token in self.FORBIDDEN:
            self.assertNotIn(token, source, "deploy client must not use %r" % token)

    def test_submission_is_the_only_side_effect(self):
        transport = RecordingTransport(
            [(201, {"runId": "deploy-run-only", "status": "accepted", "message": "ok"})]
        )
        gate.submit_manifest(manifest(), client(transport))
        self.assertEqual(transport.call_count, 1)
        self.assertTrue(transport.calls[0]["url"].endswith("/deployments"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
