# Initial verification

Verified on October 7, 2026. These results describe this implementation on one local
machine; they do not establish production readiness or a performance benchmark.

## Automated checks

- 26 pytest cases passed: diagnosis, incident lifecycle, authentication, cross-origin
  rejection, expired/mismatched approvals, changed deployments, concurrent duplicate
  execution, interrupted execution, credential scope, and atomic patch construction.
- Ruff lint and formatting checks passed.
- JavaScript syntax and launcher shell syntax checks passed.

## Browser checks

Chrome exercised the dashboard on an isolated demo server:

- All three scenarios completed checkpoint → failure → diagnosis → approval → recovery.
- Execution stayed disabled until approval.
- Recovery was clearly identified as simulated.
- No uncaught JavaScript errors occurred.
- A 390-pixel mobile viewport had no horizontal page overflow.
- Desktop and mobile screenshots were inspected. Long previews were fixed to scroll
  within their panels instead of overlapping action buttons.

The browser experiment is repeatable using `scripts/browser_smoke.py`.

## Real Kubernetes checks

Context: `docker-desktop`; node version: `v1.36.1`. The sample Deployment used
`nginx:1.28-alpine`, two replicas, and zero unavailable replicas during a rolling update.
The recovery adapter used `system:serviceaccount:kuberecovery-demo:kuberecovery`.

| Scenario | Injection to matching diagnosis evidence | Execution and verification | Result |
| --- | ---: | ---: | --- |
| Readiness-probe failure | 4.57 seconds | 0.27 seconds | Recovered |
| Image-pull failure | 4.57 seconds | 0.27 seconds | Recovered |
| Container crash loop | 4.64 seconds | 0.38 seconds | Recovered |

Each experiment refused execution before approval, restored the saved healthy pod
template, verified the observed generation and complete rollout status, and returned
the same result on duplicate execution without changing the generation again.

The very short restoration times reflect reuse of still-running healthy replicas in
this rolling-update configuration. They do not measure recovery from a complete outage,
cold image pulls, or new node provisioning. Detection timing includes the injection CLI
and polling overhead. No timing improvement against a baseline is claimed.

Scoped credentials returned `no` for patching `deployment/other-app` and reading Secrets.
The real sample workload was healthy at the end of the experiment. Docker Desktop and
the sample namespace remain available for further learning.

The real-cluster experiment is repeatable using `scripts/live_smoke.py`. Stop the live
backend first; the experiment and backend should not share SQLite state concurrently.

## Still to validate or implement

- The real-cluster test exercises the service and kubectl adapter; browser coverage is
  currently against the simulated API, not the live-token login flow.
- kind, Linux, EKS, and remote clusters were not exercised.
- AI explanations, metrics, queue processing, multiple operators, and independent
  approval roles are future milestones.
- Hosted deployment would require a different authentication and concurrency design.
