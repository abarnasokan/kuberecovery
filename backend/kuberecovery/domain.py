"""Pure diagnosis rules. Evidence is observed; root causes remain hypotheses."""

import hashlib
import json
from copy import deepcopy


class RecoveryError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fingerprint(snapshot):
    return digest({key: snapshot[key] for key in ("context", "uid", "generation", "template")})


def healthy(snapshot):
    return (
        snapshot["desired"] > 0
        and snapshot["observed_generation"] >= snapshot["generation"]
        and snapshot["updated"] == snapshot["desired"]
        and snapshot["ready"] == snapshot["desired"]
        and snapshot["available"] == snapshot["desired"]
        and snapshot["total"] == snapshot["desired"]
    )


def diagnose(snapshot):
    evidence = []
    reasons = set()
    for pod in snapshot["pods"]:
        for container in pod.get("containers", []):
            reason = container.get("reason")
            if reason:
                reasons.add(reason)
                evidence.append(
                    {"source": pod["name"], "message": reason + ": " + container.get("message", "")}
                )
    for event in snapshot["events"]:
        evidence.append({"source": event["source"], "message": event["message"]})
    if healthy(snapshot):
        code, title, explanation = (
            "healthy",
            "Deployment is healthy",
            "The current generation has completed its rollout and all desired replicas are available.",
        )
    elif reasons & {"ImagePullBackOff", "ErrImagePull"}:
        code, title, explanation = (
            "image_pull",
            "Container image cannot be pulled",
            "Check the image name, tag, registry connectivity, and pull credentials. A bad image reference is one possible cause.",
        )
    elif "CrashLoopBackOff" in reasons:
        code, title, explanation = (
            "crash_loop",
            "Container repeatedly exits",
            "Check startup configuration and previous-container logs. CrashLoopBackOff describes repeated restarts, not their root cause.",
        )
    elif any("readiness probe failed" in e["message"].lower() for e in snapshot["events"]):
        code, title, explanation = (
            "readiness_probe",
            "Readiness checks are failing",
            "Check the probe path, port, timing, and application health. The pods are not ready to receive traffic.",
        )
    else:
        code, title, explanation = (
            "unknown",
            "More evidence is needed",
            "The deployment is not fully healthy, but the available evidence does not match a supported failure pattern.",
        )
    return {"code": code, "title": title, "explanation": explanation, "evidence": evidence}


def build_plan(snapshot, checkpoint):
    if not checkpoint:
        raise RecoveryError("Save a healthy checkpoint before injecting a failure.")
    if snapshot["uid"] != checkpoint["uid"] or snapshot["context"] != checkpoint["context"]:
        raise RecoveryError("The checkpoint belongs to a different deployment or cluster context.")
    if healthy(snapshot):
        raise RecoveryError("The deployment is healthy; recovery is unnecessary.")
    if snapshot["template"] == checkpoint["template"]:
        raise RecoveryError(
            "The pod template already matches the checkpoint; investigate infrastructure or runtime failures."
        )
    if diagnose(snapshot)["code"] == "unknown":
        raise RecoveryError("No supported diagnosis; automatic recovery is not proposed.")
    return {
        "action": "restore_checkpoint_template",
        "context": snapshot["context"],
        "namespace": "kuberecovery-demo",
        "deployment": "sample-app",
        "uid": snapshot["uid"],
        "source_generation": snapshot["generation"],
        "source_fingerprint": fingerprint(snapshot),
        "source_template": deepcopy(snapshot["template"]),
        "target_template": deepcopy(checkpoint["template"]),
        "checkpoint_revision": checkpoint["revision"],
        "summary": "Restore the pod template from the saved healthy checkpoint. Kubernetes will create a new rollout.",
    }
