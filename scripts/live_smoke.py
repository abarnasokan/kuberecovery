"""Deliberately fail and recover the local sample workload using scoped credentials."""

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from kuberecovery.adapters import CONTEXTS, KubectlAdapter  # noqa: E402
from kuberecovery.domain import RecoveryError, diagnose, healthy  # noqa: E402
from kuberecovery.service import RecoveryService  # noqa: E402
from kuberecovery.store import Store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", choices=sorted(CONTEXTS), required=True)
    args = parser.parse_args()
    config_path = ROOT / ".data/live/kubeconfig"
    adapter = KubectlAdapter(args.context, config_path)
    adapter.validate_identity()
    service = RecoveryService(adapter, Store(ROOT / ".data/live/incidents.db"), "live")
    results = []
    for scenario in ["readiness_probe", "image_pull", "crash_loop"]:
        service.checkpoint()
        started = time.monotonic()
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/lab.py"),
                "inject",
                scenario,
                "--context",
                args.context,
            ],
            check=True,
        )
        deadline = time.monotonic() + 90
        while True:
            snapshot = adapter.snapshot()
            if diagnose(snapshot)["code"] == scenario:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Expected failure evidence did not appear for " + scenario)
            time.sleep(2)
        detection_seconds = time.monotonic() - started
        incident = service.create("live-smoke-" + str(uuid.uuid4()))
        if not incident["plan"]:
            raise RuntimeError(incident["blocked_reason"])
        try:
            service.execute(incident["id"])
        except RecoveryError:
            pass
        else:
            raise AssertionError("Unapproved execution was not refused")
        service.approve(incident["id"], incident["plan_digest"])
        recovery_started = time.monotonic()
        recovered = service.execute(incident["id"])
        recovery_seconds = time.monotonic() - recovery_started
        assert recovered["status"] == "recovered", recovered.get("error")
        before = adapter.snapshot()
        assert healthy(before)
        assert service.execute(incident["id"])["status"] == "recovered"
        assert adapter.snapshot()["generation"] == before["generation"]
        result = {
            "scenario": scenario,
            "status": recovered["status"],
            "detection_seconds": round(detection_seconds, 2),
            "execution_verification_seconds": round(recovery_seconds, 2),
            "incident": incident["id"],
        }
        results.append(result)
        print(json.dumps(result), flush=True)
    rbac_results = {}
    for verb, resource in [("patch", "deployment/other-app"), ("get", "secrets")]:
        response = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(config_path),
                "--context",
                args.context,
                "--namespace",
                "kuberecovery-demo",
                "--request-timeout=10s",
                "auth",
                "can-i",
                verb,
                resource,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert response.stdout.strip() == "no", response.stderr
        rbac_results[verb + " " + resource] = "denied"
    report = {
        "context": args.context,
        "flows": results,
        "rbac": rbac_results,
        "note": "Single local run; these timings are not a benchmark.",
    }
    (ROOT / ".data/live/verification.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
