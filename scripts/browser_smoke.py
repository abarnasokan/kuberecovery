"""Exercise the dashboard against an isolated demo server and save QA screenshots."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--walkthrough",
        action="store_true",
        help="Also save the six README walkthrough screenshots under docs/images/demo/",
    )
    args = parser.parse_args()
    output = ROOT / ".data" / "browser-qa"
    output.mkdir(parents=True, exist_ok=True)
    walkthrough = ROOT / "docs" / "images" / "demo"
    if args.walkthrough:
        walkthrough.mkdir(parents=True, exist_ok=True)
    port = "8091"
    with tempfile.TemporaryDirectory(prefix="kuberecovery-qa-") as data_dir:
        env = {
            **os.environ,
            "KUBERECOVERY_MODE": "demo",
            "KUBERECOVERY_DATA_DIR": data_dir,
            "PYTHONPATH": str(ROOT / "backend"),
        }
        env.pop("KUBERECOVERY_TOKEN", None)
        with tempfile.TemporaryFile() as server_log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "kuberecovery.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    port,
                ],
                cwd=ROOT,
                env=env,
                stdout=server_log,
                stderr=server_log,
            )
            try:
                deadline = time.monotonic() + 15
                while True:
                    if server.poll() is not None or time.monotonic() > deadline:
                        server_log.seek(0)
                        raise RuntimeError(server_log.read().decode())
                    try:
                        urllib.request.urlopen("http://127.0.0.1:" + port + "/health", timeout=1)
                        break
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(0.1)
                with sync_playwright() as playwright:
                    chrome = os.getenv(
                        "BROWSER_EXECUTABLE",
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    )
                    launch = {"headless": True}
                    if Path(chrome).exists():
                        launch["executable_path"] = chrome
                    browser = playwright.chromium.launch(**launch)
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 1100},
                        device_scale_factor=1,
                        timezone_id="America/Los_Angeles",
                    )

                    def capture_step(name, selector=None):
                        if not args.walkthrough or scenario != "readiness_probe":
                            return
                        if selector:
                            if selector == ".detail-panel":
                                # Show the changed probe path instead of the template's opening lines.
                                previews = page.locator(".preview pre")
                                assert previews.count() == 2
                                for index in range(2):
                                    previews.nth(index).hover()
                                    page.mouse.wheel(0, 1000)
                                    page.wait_for_function(
                                        "i => document.querySelectorAll('.preview pre')[i].scrollTop > 0",
                                        arg=index,
                                    )
                            page.locator(selector).screenshot(path=str(walkthrough / name))
                        else:
                            page.locator("header").scroll_into_view_if_needed()
                            page.screenshot(path=str(walkthrough / name), full_page=True)

                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto("http://127.0.0.1:" + port)
                    expect(page.get_by_text("SIMULATED ENVIRONMENT", exact=True)).to_be_visible()
                    expect(
                        page.get_by_role("button", name="Save healthy checkpoint")
                    ).to_be_enabled()
                    page.screenshot(path=str(output / "initial.png"), full_page=True)
                    results = []
                    for scenario, title in [
                        ("readiness_probe", "Readiness checks are failing"),
                        ("image_pull", "Container image cannot be pulled"),
                        ("crash_loop", "Container repeatedly exits"),
                    ]:
                        page.get_by_role("button", name="Save healthy checkpoint").click()
                        expect(page.get_by_role("button", name="Inject failure")).to_be_enabled()
                        capture_step("01-healthy-checkpoint.png")
                        page.get_by_label("Failure scenario", exact=True).select_option(scenario)
                        page.get_by_role("button", name="Inject failure").click()
                        expect(page.locator("#health")).to_have_text("Degraded")
                        expect(
                            page.get_by_role("button", name="Collect evidence & diagnose")
                        ).to_be_enabled()
                        capture_step("02-injected-failure.png")
                        page.get_by_role("button", name="Collect evidence & diagnose").click()
                        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                        expect(page.get_by_role("button", name="Execute recovery")).to_be_disabled()
                        expect(page.get_by_role("button", name="Approve this plan")).to_be_enabled()
                        capture_step("03-diagnosis-and-preview.png", ".detail-panel")
                        page.screenshot(
                            path=str(output / (scenario + "-preview.png")), full_page=True
                        )
                        page.get_by_role("button", name="Approve this plan").click()
                        expect(page.get_by_role("button", name="Execute recovery")).to_be_enabled()
                        capture_step("04-approved-plan.png", ".detail-panel")
                        page.get_by_role("button", name="Execute recovery").click()
                        expect(page.locator("#detail .result")).to_contain_text(
                            "Simulated recovery verified"
                        )
                        expect(page.locator("#health")).to_have_text("Healthy")
                        expect(page.locator("#error")).to_be_hidden()
                        expect(
                            page.get_by_role("button", name="Save healthy checkpoint")
                        ).to_be_enabled()
                        capture_step("05-verified-recovery.png")
                        capture_step("06-audit-trail.png", "#audit")
                        results.append({"scenario": scenario, "result": "recovered"})
                    page.screenshot(path=str(output / "recovered.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(
                        page.get_by_role("heading", name="Recover with confidence.")
                    ).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                        "Mobile page overflows horizontally"
                    )
                    page.screenshot(path=str(output / "mobile.png"), full_page=True)
                    assert not errors, errors
                    browser.close()
                    print(
                        json.dumps(
                            {
                                "browser": "Chromium",
                                "flows": results,
                                "javascript_errors": errors,
                                "mobile_overflow": False,
                                "screenshots": str(output),
                                "walkthrough": str(walkthrough) if args.walkthrough else None,
                            },
                            indent=2,
                        )
                    )
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


if __name__ == "__main__":
    main()
