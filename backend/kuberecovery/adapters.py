"""Simulated workloads and an explicitly scoped kubectl integration."""

import json
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

from .domain import RecoveryError, fingerprint, healthy

NAMESPACE = "kuberecovery-demo"
DEPLOYMENT = "sample-app"
CONTEXTS = {"kind-kuberecovery", "docker-desktop"}
GOOD_TEMPLATE = {
    "metadata": {"labels": {"app": "sample-app"}},
    "spec": {
        "containers": [
            {
                "name": "web",
                "image": "nginx:1.28-alpine",
                "ports": [{"containerPort": 80}],
                "readinessProbe": {"httpGet": {"path": "/", "port": 80}, "periodSeconds": 3},
            }
        ]
    },
}


def failed_template(template, scenario):
    result = deepcopy(template)
    container = result["spec"]["containers"][0]
    if scenario == "readiness_probe":
        container["readinessProbe"]["httpGet"]["path"] = "/intentional-failure"
    elif scenario == "image_pull":
        container["image"] = "nginx:kuberecovery-nonexistent-tag"
    elif scenario == "crash_loop":
        container["command"] = ["/bin/sh", "-c", "echo 'Intentional startup failure' >&2; exit 1"]
    else:
        raise RecoveryError("Unknown failure scenario.", 422)
    return result


class DemoAdapter:
    def __init__(self, state_path):
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save({"generation": 1, "template": GOOD_TEMPLATE, "scenario": None})

    def _load(self):
        return json.loads(self.path.read_text())

    def _save(self, value):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(value))
        temp.replace(self.path)

    def snapshot(self):
        state = self._load()
        scenario = state["scenario"]
        reason, message = {
            "readiness_probe": (
                None,
                "Readiness probe failed: HTTP probe failed with statuscode: 404",
            ),
            "image_pull": ("ImagePullBackOff", "Failed to pull image: manifest unknown"),
            "crash_loop": (
                "CrashLoopBackOff",
                "Back-off restarting failed container web; exit code 1",
            ),
            None: (None, ""),
        }[scenario]
        return {
            "context": "simulated",
            "namespace": NAMESPACE,
            "deployment": DEPLOYMENT,
            "uid": "demo-sample-app",
            "generation": state["generation"],
            "observed_generation": state["generation"],
            "revision": str(state["generation"]),
            "template": state["template"],
            "desired": 2,
            "updated": 2,
            "ready": 0 if scenario else 2,
            "available": 0 if scenario else 2,
            "total": 2,
            "pods": [
                {
                    "name": "sample-app-simulated-1",
                    "containers": [{"name": "web", "reason": reason, "message": message}],
                }
            ],
            "events": [{"source": "sample-app-simulated-1", "message": message}]
            if scenario
            else [],
            "logs": {"sample-app-simulated-1": "Intentional startup failure\n"}
            if scenario == "crash_loop"
            else {},
        }

    def inject(self, scenario):
        current = self.snapshot()
        self._save(
            {
                "generation": current["generation"] + 1,
                "template": failed_template(GOOD_TEMPLATE, scenario),
                "scenario": scenario,
            }
        )

    def restore(self, plan):
        current = self.snapshot()
        if fingerprint(current) != plan["source_fingerprint"]:
            raise RecoveryError(
                "Deployment changed after preview; collect fresh evidence and create a new plan."
            )
        self._save(
            {
                "generation": current["generation"] + 1,
                "template": plan["target_template"],
                "scenario": None,
            }
        )

    def verify(self, target, timeout=60):
        snapshot = self.snapshot()
        return healthy(snapshot) and snapshot["template"] == target


class KubectlAdapter:
    def __init__(self, context, kubeconfig):
        if context not in CONTEXTS:
            raise ValueError("Live mode supports only kind-kuberecovery or docker-desktop.")
        if not kubeconfig or not Path(kubeconfig).is_file():
            raise ValueError("Live mode requires an explicit, namespace-scoped kubeconfig file.")
        self.context = context
        self.kubeconfig = str(Path(kubeconfig).resolve())
        # The lab CLI writes a JSON kubeconfig containing only a scoped service-account token.
        try:
            config = json.loads(Path(self.kubeconfig).read_text())
            entry = next(c["context"] for c in config["contexts"] if c["name"] == context)
            cluster = next(
                c["cluster"] for c in config["clusters"] if c["name"] == entry["cluster"]
            )
            user = next(u["user"] for u in config["users"] if u["name"] == entry["user"])
            server = urlparse(cluster["server"])
            if (
                server.scheme != "https"
                or server.hostname
                not in {"127.0.0.1", "localhost", "::1", "kubernetes.docker.internal"}
                or cluster.get("insecure-skip-tls-verify")
                or entry.get("namespace") != NAMESPACE
                or set(user) != {"token"}
                or not user["token"]
            ):
                raise ValueError("Unexpected cluster or credential scope.")
        except (KeyError, StopIteration, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "Use the local, scoped kubeconfig generated by scripts/lab.py setup."
            ) from exc

    def run(self, *args, timeout=15):
        command = [
            "kubectl",
            "--kubeconfig",
            self.kubeconfig,
            "--context",
            self.context,
            "--namespace",
            NAMESPACE,
            "--request-timeout=10s",
            *args,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise RecoveryError(
                "kubectl is unavailable or the Kubernetes request timed out.", 503
            ) from exc
        if result.returncode:
            # Surface cluster errors, never the command (which could contain credentials).
            raise RecoveryError("Kubernetes request failed: " + result.stderr.strip()[:600], 503)
        return result.stdout

    def validate_identity(self):
        info = json.loads(self.run("auth", "whoami", "-o", "json"))
        expected = "system:serviceaccount:kuberecovery-demo:kuberecovery"
        if info.get("status", {}).get("userInfo", {}).get("username") != expected:
            raise ValueError("Live mode requires the lab's kuberecovery service-account identity.")

    def snapshot(self, deadline=None):
        def query(*args):
            remaining = 15 if deadline is None else min(15, deadline - time.monotonic())
            if remaining <= 0:
                raise RecoveryError("The verification deadline was reached.", 503)
            return self.run(*args, timeout=remaining)

        deployment = json.loads(query("get", "deployment", DEPLOYMENT, "-o", "json"))
        replicasets = json.loads(query("get", "replicasets", "-l", "app=sample-app", "-o", "json"))[
            "items"
        ]
        uid = deployment["metadata"]["uid"]
        rs_uids = {
            rs["metadata"]["uid"]
            for rs in replicasets
            if any(o["uid"] == uid for o in rs["metadata"].get("ownerReferences", []))
        }
        pods = json.loads(query("get", "pods", "-l", "app=sample-app", "-o", "json"))["items"]
        pods = [
            p
            for p in pods
            if any(o["uid"] in rs_uids for o in p["metadata"].get("ownerReferences", []))
        ]
        pod_uids = {p["metadata"]["uid"] for p in pods}
        all_events = json.loads(query("get", "events", "-o", "json"))["items"]
        events = [
            {"source": e["involvedObject"].get("name", ""), "message": e.get("message", "")}
            for e in all_events
            if e.get("involvedObject", {}).get("uid") in pod_uids | {uid}
        ]
        # Ignore old ReplicaSet failures after a newer rollout; readiness evidence is current pods only.
        current_pods = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        summaries, logs = [], {}
        for pod in current_pods:
            containers = []
            for c in pod.get("status", {}).get("containerStatuses", []):
                waiting = c.get("state", {}).get("waiting", {})
                containers.append(
                    {
                        "name": c["name"],
                        "reason": waiting.get("reason"),
                        "message": waiting.get("message", ""),
                    }
                )
            name = pod["metadata"]["name"]
            summaries.append({"name": name, "containers": containers})
            if any(c.get("reason") == "CrashLoopBackOff" for c in containers):
                try:
                    logs[name] = query(
                        "logs", name, "-c", "web", "--previous", "--tail=30", "--limit-bytes=4096"
                    )
                except RecoveryError:
                    logs[name] = "Previous-container logs are unavailable."
        spec, status, metadata = (
            deployment["spec"],
            deployment.get("status", {}),
            deployment["metadata"],
        )
        return {
            "context": self.context,
            "namespace": NAMESPACE,
            "deployment": DEPLOYMENT,
            "uid": uid,
            "generation": metadata["generation"],
            "observed_generation": status.get("observedGeneration", 0),
            "revision": metadata.get("annotations", {}).get(
                "deployment.kubernetes.io/revision", "unknown"
            ),
            "template": spec["template"],
            "desired": spec.get("replicas", 1),
            "updated": status.get("updatedReplicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
            "total": status.get("replicas", 0),
            "pods": summaries,
            "events": events[-30:],
            "logs": logs,
        }

    def restore(self, plan):
        # The API server checks all three preconditions atomically with the replacement.
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": plan["uid"]},
            {"op": "test", "path": "/metadata/generation", "value": plan["source_generation"]},
            {"op": "test", "path": "/spec/template", "value": plan["source_template"]},
            {"op": "replace", "path": "/spec/template", "value": plan["target_template"]},
        ]
        self.run("patch", "deployment", DEPLOYMENT, "--type=json", "-p", json.dumps(patch))

    def verify(self, target, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                snapshot = self.snapshot(deadline=deadline)
            except RecoveryError:
                return False
            if snapshot["template"] != target:
                return False
            if healthy(snapshot):
                return True
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        return False
