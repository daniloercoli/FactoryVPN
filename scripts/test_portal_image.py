"""Smoke-test the built portal image over HTTPS, without the application stack."""

import os
from html import unescape
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import time
import urllib.request
from uuid import uuid4

from scripts.portal_e2e import certificate
from scripts.test_with_postgres import docker

ROOT = Path(__file__).resolve().parents[1]


def main():
    # Set APP_NAME to the same value passed to the image build for custom labels.
    app_name = os.environ.get("APP_NAME", "FactoryVPN").strip()
    name = "factoryvpn-portal-test-" + uuid4().hex[:12]
    container = None
    with tempfile.TemporaryDirectory(prefix="factoryvpn-nginx-") as folder:
        cert, _ = certificate(folder)
        context = ssl.create_default_context(cafile=str(cert))
        try:
            container = docker(
                "create",
                "--name",
                name,
                "--rm",
                "--add-host",
                "api:127.0.0.1",
                "--label",
                "eu.ercoliconsulting.factoryvpn.purpose=tests",
                "--publish",
                "127.0.0.1::443",
                "--mount",
                f"type=bind,source={folder},target=/etc/nginx/ssl,readonly",
                "factoryvpn-portal:test",
                stdout=subprocess.PIPE,
            ).stdout.strip()
            docker("start", container, stdout=subprocess.DEVNULL)
            binding = docker("port", container, "443/tcp", stdout=subprocess.PIPE).stdout.strip()
            assert binding.startswith("127.0.0.1:") and "\n" not in binding
            origin = "https://" + binding
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urllib.request.urlopen(
                        origin + "/app/login", context=context, timeout=2
                    ) as response:
                        html = response.read().decode()
                        headers = response.headers
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.2)
            assert headers["Cache-Control"] == "no-store"
            title = re.search(r"<title>(.*?)</title>", html)
            assert title and unescape(title.group(1)) == f"{app_name} · Portale"
            assert "text/html" in headers["Content-Type"]
            assert headers["Referrer-Policy"] == "no-referrer"
            assert headers["X-Frame-Options"] == "DENY"
            assert "script-src 'self'" in headers["Content-Security-Policy"]
            assert "unsafe-inline" not in headers["Content-Security-Policy"]
            for path in re.findall(r'(?:src|href)="(/app/assets/[^\"]+)"', html):
                with urllib.request.urlopen(origin + path, context=context, timeout=2) as response:
                    assert response.status == 200
                    assert "immutable" in response.headers["Cache-Control"]
                    assert "application/octet-stream" not in response.headers["Content-Type"]
            with urllib.request.urlopen(
                origin + "/app/users/arbitrary-route", context=context
            ) as response:
                assert response.read().decode() == html
            with urllib.request.urlopen(origin + "/app", context=context) as response:
                assert response.url == origin + "/app/"
            try:
                urllib.request.urlopen(origin + "/app/assets/missing.js", context=context)
                raise AssertionError("Missing assets must not return the SPA")
            except urllib.error.HTTPError as error:
                assert error.code == 404
            # Run the actual compiled bundle under the production CSP. Only API reads
            # are fulfilled with unauthenticated test responses; no backend is started.
            code = """
import { chromium } from '@playwright/test';
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ ignoreHTTPSErrors: true });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => { window.cspErrors = []; document.addEventListener('securitypolicyviolation', e => window.cspErrors.push(e.violatedDirective)); });
  await page.route('**/api/v1/**', route => route.fulfill({ status: route.request().url().endsWith('/web/session') ? 401 : 200, contentType: 'application/json', body: route.request().url().endsWith('/web/session') ? '{"detail":"No session"}' : '[]' }));
  await page.goto(process.env.FACTORYVPN_IMAGE_URL + '/app/login');
  await page.getByRole('heading', { name: 'Accedi a ' + process.env.APP_NAME, exact: true }).waitFor();
  if (errors.length || (await page.evaluate(() => window.cspErrors)).length) throw new Error('Bundle or CSP failure: ' + errors.join(';'));
} finally { await browser.close(); }
"""
            subprocess.run(
                ["node", "--input-type=module", "-e", code],
                cwd=ROOT / "portal",
                env={**os.environ, "FACTORYVPN_IMAGE_URL": origin, "APP_NAME": app_name},
                check=True,
                timeout=30,
            )
            print(
                "Portal image: HTTPS, SPA routes, MIME types, caching, CSP and compiled browser bundle passed."
            )
        finally:
            if container:
                docker("rm", "--force", container, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
