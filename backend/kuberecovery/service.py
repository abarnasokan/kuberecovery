"""Incident lifecycle and approval enforcement, independent of HTTP."""

import logging
import threading
import time
import uuid

from .domain import RecoveryError, build_plan, diagnose, digest, fingerprint, healthy


class RecoveryService:
    def __init__(self, adapter, store, mode="demo", clock=time.time):
        self.adapter, self.store, self.mode, self.clock = adapter, store, mode, clock
        # One server worker: serialize cluster mutations and incident state transitions.
        self.lock = threading.RLock()

    def audit(self, action, details=None, actor="local-operator"):
        return (self.clock(), action, actor, details or {})

    def overview(self):
        with self.lock:
            snapshot = self.adapter.snapshot()
            checkpoint = self.store.checkpoint()
            return {
                "mode": self.mode,
                "snapshot": snapshot,
                "diagnosis": diagnose(snapshot),
                "checkpoint": checkpoint,
                "incidents": self.store.incidents(),
                "audit": self.store.audit(),
            }

    def checkpoint(self):
        with self.lock:
            snapshot = self.adapter.snapshot()
            if not healthy(snapshot):
                raise RecoveryError("Checkpoint refused: the current rollout is not fully healthy.")
            checkpoint = {key: snapshot[key] for key in ("context", "uid", "revision", "template")}
            checkpoint["created_at"] = self.clock()
            self.store.save_checkpoint(
                checkpoint, self.audit("checkpoint_saved", {"revision": snapshot["revision"]})
            )
            return checkpoint

    def inject(self, scenario):
        with self.lock:
            if self.mode != "demo":
                raise RecoveryError(
                    "Live failure injection is available only through the lab CLI.", 403
                )
            if not self.store.checkpoint():
                raise RecoveryError("Save a healthy checkpoint before injecting a failure.")
            self.adapter.inject(scenario)
            self.store.record(None, self.audit("demo_failure_injected", {"scenario": scenario}))
            return self.adapter.snapshot()

    def create(self, request_key):
        with self.lock:
            existing = self.store.by_key(request_key)
            if existing:
                return existing
            snapshot = self.adapter.snapshot()
            diagnosis = diagnose(snapshot)
            plan, blocked = None, None
            try:
                plan = build_plan(snapshot, self.store.checkpoint())
            except RecoveryError as exc:
                blocked = str(exc)
            now = self.clock()
            incident = {
                "id": str(uuid.uuid4()),
                "request_key": request_key,
                "created_at": now,
                "status": "awaiting_approval" if plan else "diagnosed",
                "snapshot": snapshot,
                "diagnosis": diagnosis,
                "plan": plan,
                "blocked_reason": blocked,
                "plan_digest": digest(plan) if plan else None,
                "expires_at": now + 300 if plan else None,
            }
            self.store.save_incident(
                incident, self.audit("incident_diagnosed", {"code": diagnosis["code"]})
            )
            return incident

    def approve(self, identifier, plan_digest):
        with self.lock:
            incident = self.store.incident(identifier)
            if incident["status"] not in {"awaiting_approval", "approved"}:
                raise RecoveryError("This incident is not awaiting approval.")
            if self.clock() >= incident["expires_at"]:
                self.store.record(
                    identifier, self.audit("approval_rejected", {"reason": "expired"})
                )
                raise RecoveryError("Preview expired. Create a fresh incident.")
            if plan_digest != incident["plan_digest"]:
                self.store.record(
                    identifier, self.audit("approval_rejected", {"reason": "plan_mismatch"})
                )
                raise RecoveryError("Approval must match the exact previewed plan.")
            if incident["status"] == "approved":
                return incident
            incident.update(
                status="approved", approved_at=self.clock(), approved_by="local-operator"
            )
            self.store.save_incident(
                incident, self.audit("recovery_approved", {"plan_digest": plan_digest})
            )
            return incident

    def execute(self, identifier):
        with self.lock:
            incident = self.store.incident(identifier)
            if incident["status"] in {"recovered", "failed", "verification_required"}:
                return incident  # Never repeat a cluster mutation on a duplicate request.
            if incident["status"] != "approved":
                self.store.record(
                    identifier, self.audit("execution_rejected", {"reason": "not_approved"})
                )
                raise RecoveryError("Approve the preview before executing recovery.")
            if self.clock() >= incident["expires_at"]:
                self.store.record(
                    identifier, self.audit("execution_rejected", {"reason": "expired"})
                )
                raise RecoveryError("Approval expired. Create a fresh incident.")
            plan = incident["plan"]
            if digest(plan) != incident["plan_digest"]:
                raise RecoveryError("The approved plan has changed.")
            if fingerprint(self.adapter.snapshot()) != plan["source_fingerprint"]:
                self.store.record(
                    identifier, self.audit("execution_rejected", {"reason": "deployment_changed"})
                )
                raise RecoveryError("Deployment changed after preview. Create a fresh incident.")
            incident.update(status="executing", execution_started_at=self.clock())
            self.store.save_incident(incident, self.audit("recovery_started"))
            try:
                self.adapter.restore(plan)
                verified = self.adapter.verify(plan["target_template"], timeout=60)
                incident["status"] = "recovered" if verified else "verification_required"
                if not verified:
                    incident["error"] = (
                        "Recovery did not meet health checks within the verification window. Inspect the deployment before taking another action."
                    )
            except RecoveryError as exc:
                # A timeout can happen after the API server accepted a patch. Do not imply no mutation.
                incident.update(status="verification_required", error=str(exc))
            except Exception:
                logging.exception("Unexpected error during recovery of incident %s", identifier)
                incident.update(
                    status="verification_required",
                    error="Unexpected recovery error; inspect cluster state before taking another action.",
                )
            incident["completed_at"] = self.clock()
            self.store.save_incident(
                incident, self.audit("recovery_completed", {"status": incident["status"]})
            )
            return incident
