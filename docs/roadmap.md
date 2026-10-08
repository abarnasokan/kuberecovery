# Build and learning milestones

## 1. Working local recovery flow — implemented

Run all three failures in demo mode. Read `domain.py` first, then trace an incident
through `service.py` and `app.py`. Understand why an old revision cannot simply be
assumed healthy and why a successful patch is not a successful recovery.

## 2. Verify real Kubernetes behavior — initial pass complete

Start Docker and create the local cluster. Run setup, start live mode, and save a
checkpoint. For each failure:

- Record the Deployment generation and checkpoint revision.
- Inject the failure; wait for supported pod/event evidence.
- Confirm the diagnosis uses real pod names and events.
- Review and approve the restoration, then verify a completed rollout.
- Confirm all desired replicas are updated, ready, and available.
- Inspect the audit trail and Kubernetes revision history.

Also verify:

- The scoped service account cannot patch a different Deployment or read Secrets.
- Recovery without approval is refused.
- A newly injected failure after preview invalidates that plan.
- An expired preview is refused.
- Duplicate execution requests create only one recovery rollout.
- Restarting the server preserves history.

Capture actual time to detect and recover. Keep simulated measurements separate.

## 3. Observability and reproducibility

Add Prometheus counters for diagnosed incidents, rejected approvals, and verified
recoveries. Add a duration histogram and Grafana dashboard. Define each measurement:
injection-to-diagnosis time differs from approval-to-recovery time. Automate a local
integration scenario and check it in CI against a disposable kind cluster.

## 4. AI explanations

Use a model to summarize already collected evidence and cite its sources. Keep plan
construction and validation deterministic. Treat pod logs and events as untrusted
input; a log message must never instruct the agent to run commands. Test hallucinated
causes, missing evidence, and prompt injection before adding any model-generated actions.

## 5. Portfolio release

Record a 2–3 minute live demo, publish an architecture diagram and actual experiment
results, and add a concise README walkthrough. A hosted/EKS version comes after the
local behavior is verified and would need OIDC identities, separate approval roles,
managed persistence, and asynchronous jobs.

The initial pass completed all three failures and approved recoveries against Docker
Desktop Kubernetes, checked two RBAC denials, and exercised the dashboard in Chrome.
The stale-plan and expiry cases are covered by automated tests; repeat them on the
live cluster as an additional exercise. See [verification.md](verification.md).
