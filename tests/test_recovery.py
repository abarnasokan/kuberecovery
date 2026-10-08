import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from kuberecovery.adapters import DemoAdapter, KubectlAdapter
from kuberecovery.app import create_app
from kuberecovery.domain import RecoveryError, build_plan, diagnose, healthy
from kuberecovery.service import RecoveryService
from kuberecovery.store import Store


@pytest.fixture
def service(tmp_path):
    return RecoveryService(DemoAdapter(tmp_path / "cluster.json"), Store(tmp_path / "incidents.db"))


def prepared(service, scenario="readiness_probe", key="test-request-1"):
    service.checkpoint()
    service.inject(scenario)
    return service.create(key)


def kubeconfig(tmp_path):
    config = tmp_path / "kubeconfig"
    config.write_text(
        json.dumps(
            {
                "clusters": [{"name": "local", "cluster": {"server": "https://127.0.0.1:6443"}}],
                "contexts": [
                    {
                        "name": name,
                        "context": {
                            "cluster": "local",
                            "user": "scoped",
                            "namespace": "kuberecovery-demo",
                        },
                    }
                    for name in ["kind-kuberecovery", "docker-desktop"]
                ],
                "users": [{"name": "scoped", "user": {"token": "test-service-account-token"}}],
            }
        )
    )
    return config


@pytest.mark.parametrize("scenario", ["readiness_probe", "image_pull", "crash_loop"])
def test_complete_recovery_flow(service, scenario):
    incident = prepared(service, scenario)
    assert incident["diagnosis"]["code"] == scenario
    assert incident["diagnosis"]["evidence"]
    service.approve(incident["id"], incident["plan_digest"])
    result = service.execute(incident["id"])
    assert result["status"] == "recovered"
    assert healthy(service.adapter.snapshot())
    assert {a["action"] for a in service.store.audit()} >= {
        "checkpoint_saved",
        "recovery_approved",
        "recovery_started",
        "recovery_completed",
    }


def test_execution_requires_approval(service):
    incident = prepared(service)
    before = service.adapter.snapshot()
    with pytest.raises(RecoveryError, match="Approve"):
        service.execute(incident["id"])
    assert service.adapter.snapshot() == before


def test_approval_is_bound_to_plan(service):
    incident = prepared(service)
    with pytest.raises(RecoveryError, match="exact previewed plan"):
        service.approve(incident["id"], "a" * 64)
    assert service.store.incident(incident["id"])["status"] == "awaiting_approval"


@pytest.mark.parametrize("stage", ["approve", "execute"])
def test_expired_plan_never_executes(service, stage):
    incident = prepared(service)
    if stage == "execute":
        service.approve(incident["id"], incident["plan_digest"])
    service.clock = lambda: incident["expires_at"]
    with pytest.raises(RecoveryError, match="expired"):
        if stage == "approve":
            service.approve(incident["id"], incident["plan_digest"])
        else:
            service.execute(incident["id"])
    assert not healthy(service.adapter.snapshot())


def test_stale_preview_refuses_mutation(service):
    incident = prepared(service)
    service.approve(incident["id"], incident["plan_digest"])
    service.inject("image_pull")
    before = service.adapter.snapshot()
    with pytest.raises(RecoveryError, match="changed after preview"):
        service.execute(incident["id"])
    assert service.adapter.snapshot() == before


def test_duplicate_incident_requests_return_same_incident(service):
    incident = prepared(service)
    assert service.create(incident["request_key"])["id"] == incident["id"]
    assert len(service.store.incidents()) == 1


def test_concurrent_execute_mutates_once(service):
    incident = prepared(service)
    service.approve(incident["id"], incident["plan_digest"])
    generation = service.adapter.snapshot()["generation"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(service.execute, [incident["id"], incident["id"]]))
    assert all(result["status"] == "recovered" for result in results)
    assert service.adapter.snapshot()["generation"] == generation + 1
    assert sum(a["action"] == "recovery_started" for a in service.store.audit()) == 1


def test_unhealthy_checkpoint_refused(service):
    prepared(service)
    before = service.store.checkpoint()
    with pytest.raises(RecoveryError, match="not fully healthy"):
        service.checkpoint()
    assert service.store.checkpoint() == before


def test_healthy_deployment_does_not_get_recovery_plan(service):
    service.checkpoint()
    assert service.create("healthy-request")["plan"] is None


def test_unknown_failure_does_not_get_recovery_plan(service):
    checkpoint = service.checkpoint()
    snapshot = service.adapter.snapshot()
    snapshot["ready"] = 0
    snapshot["template"]["spec"]["containers"][0]["image"] = "other-image"
    assert diagnose(snapshot)["code"] == "unknown"
    with pytest.raises(RecoveryError, match="supported diagnosis"):
        build_plan(snapshot, checkpoint)


def test_recreated_deployment_invalidates_checkpoint(service):
    checkpoint = service.checkpoint()
    service.inject("crash_loop")
    snapshot = service.adapter.snapshot()
    snapshot["uid"] = "new-deployment"
    with pytest.raises(RecoveryError, match="different deployment"):
        build_plan(snapshot, checkpoint)


def test_verification_timeout_is_not_claimed_as_success(service):
    incident = prepared(service)
    service.approve(incident["id"], incident["plan_digest"])
    service.adapter.verify = Mock(return_value=False)
    assert service.execute(incident["id"])["status"] == "verification_required"
    generation = service.adapter.snapshot()["generation"]
    service.execute(incident["id"])
    assert service.adapter.snapshot()["generation"] == generation


def test_restart_preserves_incidents_and_flags_interrupted_execution(service):
    incident = prepared(service)
    incident["status"] = "executing"
    service.store.save_incident(incident, service.audit("recovery_started"))
    reopened = Store(service.store.path, recover_interrupted=True)
    assert reopened.incident(incident["id"])["status"] == "verification_required"
    assert reopened.checkpoint() is not None
    assert reopened.audit()[0]["action"] == "execution_interrupted"


def test_health_requires_observed_generation_and_complete_rollout(service):
    snapshot = service.adapter.snapshot()
    assert healthy(snapshot)
    stale = deepcopy(snapshot)
    stale["observed_generation"] = 0
    assert not healthy(stale)
    stale = deepcopy(snapshot)
    stale["total"] = 3
    assert not healthy(stale)


def test_http_auth_and_cross_origin_guards(service):
    client = TestClient(create_app(service, token="test-api-token-with-24-characters"))
    assert client.get("/api/overview").status_code == 401
    headers = {"Authorization": "Bearer test-api-token-with-24-characters"}
    assert client.get("/api/overview", headers=headers).status_code == 200
    assert (
        client.post(
            "/api/checkpoints", json={}, headers={**headers, "Origin": "https://foreign.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/checkpoints", headers=headers).status_code == 415
    assert client.post("/api/checkpoints", json={}, headers=headers).status_code == 200
    assert client.get("/api/overview", headers={"Host": "foreign.example"}).status_code == 400


def test_http_workflow_and_invalid_inputs(service):
    client = TestClient(create_app(service))
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.post("/api/checkpoints", json={}).status_code == 200
    assert (
        client.post("/api/demo/failures", json={"scenario": "delete_everything"}).status_code == 422
    )
    assert (
        client.post("/api/demo/failures", json={"scenario": "readiness_probe"}).status_code == 200
    )
    incident = client.post("/api/incidents", json={"request_key": "http-request-1"}).json()
    path = "/api/incidents/" + incident["id"]
    assert client.post(path + "/execute", json={}).status_code == 409
    assert (
        client.post(path + "/approve", json={"plan_digest": incident["plan_digest"]}).status_code
        == 200
    )
    assert client.post(path + "/execute", json={}).json()["status"] == "recovered"
    assert client.post("/api/incidents/no-such-id/execute", json={}).status_code == 404


def test_live_mode_fails_closed_without_token_or_allowed_context(service, tmp_path):
    service.mode = "live"
    with pytest.raises(ValueError, match="strong API token"):
        create_app(service)
    config = kubeconfig(tmp_path)
    with pytest.raises(ValueError, match="only kind-kuberecovery or docker-desktop"):
        KubectlAdapter("production", config)
    with pytest.raises(RecoveryError, match="only through the lab CLI"):
        service.inject("readiness_probe")


def test_kubectl_rollback_has_atomic_preconditions(service, tmp_path):
    incident = prepared(service)
    config = kubeconfig(tmp_path)
    adapter = KubectlAdapter("kind-kuberecovery", config)
    adapter.snapshot = Mock(return_value=service.adapter.snapshot())
    adapter.run = Mock(return_value="patched")
    adapter.restore(incident["plan"])
    args = adapter.run.call_args.args
    assert args[:4] == ("patch", "deployment", "sample-app", "--type=json")
    patch = json.loads(args[-1])
    assert [item["path"] for item in patch[:3]] == [
        "/metadata/uid",
        "/metadata/generation",
        "/spec/template",
    ]
    assert all(item["op"] == "test" for item in patch[:3])
    assert patch[-1]["value"] == incident["plan"]["target_template"]


def test_kubectl_commands_always_use_explicit_scope(tmp_path, monkeypatch):
    config = kubeconfig(tmp_path)
    adapter = KubectlAdapter("docker-desktop", config)
    run = Mock(return_value=Mock(returncode=0, stdout="{}"))
    monkeypatch.setattr("kuberecovery.adapters.subprocess.run", run)
    adapter.run("get", "deployment", "sample-app", "-o", "json")
    command = run.call_args.args[0]
    assert command[:9] == [
        "kubectl",
        "--kubeconfig",
        str(config),
        "--context",
        "docker-desktop",
        "--namespace",
        "kuberecovery-demo",
        "--request-timeout=10s",
        "get",
    ]
    assert run.call_args.kwargs["timeout"] == 15


def test_live_identity_must_be_lab_service_account(tmp_path):
    adapter = KubectlAdapter("docker-desktop", kubeconfig(tmp_path))
    adapter.run = Mock(return_value=json.dumps({"status": {"userInfo": {"username": "admin"}}}))
    with pytest.raises(ValueError, match="service-account identity"):
        adapter.validate_identity()
    adapter.run = Mock(
        return_value=json.dumps(
            {
                "status": {
                    "userInfo": {"username": "system:serviceaccount:kuberecovery-demo:kuberecovery"}
                }
            }
        )
    )
    adapter.validate_identity()


def test_opening_store_without_startup_recovery_does_not_interrupt_execution(service):
    incident = prepared(service)
    incident["status"] = "executing"
    service.store.save_incident(incident, service.audit("recovery_started"))
    assert Store(service.store.path).incident(incident["id"])["status"] == "executing"


@pytest.mark.parametrize(
    "field,value",
    [("server", "https://production.example:6443"), ("insecure-skip-tls-verify", True)],
)
def test_live_kubeconfig_rejects_remote_or_unverified_cluster(tmp_path, field, value):
    config = kubeconfig(tmp_path)
    contents = json.loads(config.read_text())
    contents["clusters"][0]["cluster"][field] = value
    config.write_text(json.dumps(contents))
    with pytest.raises(ValueError, match="local, scoped kubeconfig"):
        KubectlAdapter("docker-desktop", config)
