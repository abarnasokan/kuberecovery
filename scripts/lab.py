#!/usr/bin/env python3
"""Prepare or deliberately break only the project's local sample workload."""

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from kuberecovery.adapters import CONTEXTS, DEPLOYMENT, NAMESPACE, failed_template  # noqa: E402


def kubectl(context, *args, timeout=30):
    result = subprocess.run(
        ["kubectl", "--context", context, "--request-timeout=15s", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:600])
    return result.stdout


def local_cluster(context):
    config = json.loads(kubectl(context, "config", "view", "--minify", "--flatten", "-o", "json"))
    cluster = config["clusters"][0]["cluster"]
    server = urlparse(cluster["server"])
    if (
        server.scheme != "https"
        or server.hostname not in {"localhost", "127.0.0.1", "::1", "kubernetes.docker.internal"}
        or cluster.get("insecure-skip-tls-verify")
    ):
        raise RuntimeError("This command requires a local cluster endpoint with TLS verification.")
    return cluster


def private_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(text)
    path.chmod(0o600)


def setup(context):
    local_cluster(context)
    existing = kubectl(context, "get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json")
    if (
        existing
        and json.loads(existing)["metadata"].get("labels", {}).get("app.kubernetes.io/part-of")
        != "kuberecovery"
    ):
        raise RuntimeError("The namespace already exists and is not labeled as this project's lab.")
    print("Applying the local sample app and namespace-scoped recovery role…")
    kubectl(context, "apply", "-f", str(ROOT / "k8s" / "lab.yaml"))
    kubectl(
        context,
        "-n",
        NAMESPACE,
        "rollout",
        "status",
        "deployment/" + DEPLOYMENT,
        "--timeout=120s",
        timeout=130,
    )
    # Raw config is captured privately for the CA data; no existing user credentials are copied.
    source = json.loads(
        kubectl(context, "config", "view", "--minify", "--flatten", "--raw", "-o", "json")
    )
    cluster = source["clusters"][0]["cluster"]
    sa_token = kubectl(
        context, "-n", NAMESPACE, "create", "token", "kuberecovery", "--duration=1h"
    ).strip()
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": context,
        "clusters": [{"name": "local-lab", "cluster": cluster}],
        "users": [{"name": "kuberecovery", "user": {"token": sa_token}}],
        "contexts": [
            {
                "name": context,
                "context": {"cluster": "local-lab", "user": "kuberecovery", "namespace": NAMESPACE},
            }
        ],
    }
    private_file(ROOT / ".data/live/kubeconfig", json.dumps(config, indent=2))
    token_path = ROOT / ".data/live/api-token"
    if not token_path.exists():
        private_file(token_path, secrets.token_urlsafe(32))
    print("Lab ready. Scoped Kubernetes credentials request a one-hour lifetime.")
    print("Run: bash scripts/run.sh live")
    print("Read .data/live/api-token locally and paste it into the dashboard token field.")
    print("Save a healthy checkpoint in the dashboard before injecting a failure.")


def inject(context, scenario):
    local_cluster(context)
    namespace = json.loads(kubectl(context, "get", "namespace", NAMESPACE, "-o", "json"))
    if namespace["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != "kuberecovery":
        raise RuntimeError("Refusing to modify a namespace that is not this project's lab.")
    deployment = json.loads(
        kubectl(context, "-n", NAMESPACE, "get", "deployment", DEPLOYMENT, "-o", "json")
    )
    db_path = ROOT / ".data/live/incidents.db"
    if not db_path.exists():
        raise RuntimeError("Start live mode and save a healthy checkpoint in the dashboard first.")
    from kuberecovery.store import Store

    checkpoint = Store(db_path).checkpoint()
    if (
        not checkpoint
        or checkpoint["uid"] != deployment["metadata"]["uid"]
        or checkpoint["context"] != context
    ):
        raise RuntimeError("A matching healthy checkpoint is required before failure injection.")
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": deployment["metadata"]["uid"]},
        {
            "op": "test",
            "path": "/metadata/generation",
            "value": deployment["metadata"]["generation"],
        },
        {
            "op": "replace",
            "path": "/spec/template",
            "value": failed_template(checkpoint["template"], scenario),
        },
    ]
    kubectl(
        context,
        "-n",
        NAMESPACE,
        "patch",
        "deployment",
        DEPLOYMENT,
        "--type=json",
        "-p",
        json.dumps(patch),
    )
    print(
        "Injected "
        + scenario
        + ". Wait for the failure to appear, then refresh and diagnose in the dashboard."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser(
        "setup", help="Deploy the sample app and generate scoped credentials"
    )
    setup_parser.add_argument("--context", choices=sorted(CONTEXTS), required=True)
    injection = commands.add_parser("inject", help="Deliberately break the sample Deployment")
    injection.add_argument("scenario", choices=["readiness_probe", "image_pull", "crash_loop"])
    injection.add_argument("--context", choices=sorted(CONTEXTS), required=True)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup(args.context)
        else:
            inject(args.context, args.scenario)
    except (RuntimeError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
