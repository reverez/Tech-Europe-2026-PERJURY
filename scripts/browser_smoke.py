"""Real-browser smoke of the demo UI against the real API (headless Chrome via CDP).

Starts `serve_demo.py --mock-models` (deterministic model outputs, real pytest execution), drives the
page with the primary CTA, waits for the terminal state, asserts what a judge sees, then reloads to
prove #run=<id> restoration. Saves a screenshot. Needs a Chrome/Chromium headless binary.

    python scripts/browser_smoke.py [--chrome PATH] [--shot PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_chrome(explicit: str | None) -> str:
    candidates = [explicit] if explicit else []
    candidates += [shutil.which(n) for n in ("chrome-headless-shell", "chromium", "google-chrome")]
    candidates += glob.glob(
        os.path.expanduser(
            "~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"
        )
    )
    for c in candidates:
        if c and os.path.exists(c):
            return c
    raise SystemExit("ERROR: no headless Chrome found (pass --chrome PATH)")


class Page:
    def __init__(self, ws) -> None:
        self.ws, self.n, self.errors = ws, 0, []

    async def call(self, method: str, **params):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("method") == "Runtime.exceptionThrown":
                self.errors.append(msg["params"]["exceptionDetails"].get("text", "exception"))
            if msg.get("method") == "Log.entryAdded" and msg["params"]["entry"]["level"] == "error":
                self.errors.append(msg["params"]["entry"]["text"])
            if msg.get("id") == self.n:
                return msg.get("result", {})

    async def js(self, expr: str):
        r = await self.call(
            "Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True
        )
        return r.get("result", {}).get("value")

    async def wait_for(self, expr: str, timeout: float = 90.0):
        end = time.time() + timeout
        while time.time() < end:
            value = await self.js(expr)
            if value:
                return value
            await asyncio.sleep(0.3)
        raise SystemExit(f"FAIL: timed out waiting for: {expr}")


FACTS = """(() => {
  const q = (s) => document.querySelector(s);
  const all = (s) => [...document.querySelectorAll(s)];
  return {
    verdict: q('[data-verdict]')?.dataset.verdict,
    cards: all('.mut').map((c) => [c.dataset.mutation, c.dataset.status]),
    picked: q('.mut.picked')?.dataset.mutation,
    candidate: q('[data-candidate]')?.textContent,
    delta: q('[data-delta]')?.dataset.delta,
    scores: all('.score strong').map((e) => e.textContent),
    proof: all('.proof .badge').map((e) => e.textContent),
    banner: !document.getElementById('sim-banner').hidden,
    alert: !document.getElementById('alert').hidden,
    stages: all('#pipeline li').map((l) => l.className),
    runMeta: document.getElementById('run-meta').textContent,
    hash: location.hash,
    cta: document.getElementById('cta').textContent,
    strayText: /\\b(null|undefined|NaN)\\b/.test(document.body.innerText),
  };
})()"""


async def drive(url: str, chrome: str, shot: Path) -> None:
    port = free_port()
    proc = subprocess.Popen(  # noqa: ASYNC220 - script, blocking is fine
        [chrome, "--no-sandbox", "--headless", f"--remote-debugging-port={port}",
         "--window-size=1280,1600", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(50):
            try:
                targets = json.load(
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list")  # noqa: ASYNC210
                )
                ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
                break
            except (OSError, StopIteration):
                await asyncio.sleep(0.2)
        else:
            raise SystemExit("FAIL: could not attach to Chrome")
        async with websockets.connect(ws_url, max_size=None) as ws:
            page = Page(ws)
            for domain in ("Page", "Runtime", "Log"):
                await page.call(f"{domain}.enable")
            await page.call("Page.navigate", url=f"{url}/demo")
            await page.wait_for("document.getElementById('cta') !== null")
            assert await page.js("document.getElementById('sim-banner').hidden") is True
            t0 = time.time()
            await page.js("document.getElementById('cta').click()")
            await page.wait_for("!!document.querySelector('[data-verdict]')")
            await page.wait_for("document.getElementById('cta').textContent === 'Run again'")
            elapsed = time.time() - t0
            facts = await page.js(FACTS)
            shot.write_bytes(
                __import__("base64").b64decode((await page.call("Page.captureScreenshot"))["data"])
            )
            print(f"first run: {elapsed:.1f}s  facts={json.dumps(facts)}")

            statuses = dict(facts["cards"])
            assert facts["verdict"] == "verified", facts
            assert facts["picked"] == "M01" and statuses["M01"] == "survived", facts
            assert sum(v == "killed" for v in statuses.values()) == 5, facts
            assert sum(v == "survived" for v in statuses.values()) == 3, facts
            assert facts["proof"] == ["PASS", "TEST FAIL"], facts
            assert facts["candidate"] == "examples/refund/test_perjury_M01.py", facts
            assert facts["scores"] == ["62.5%", "75%"], facts
            assert facts["delta"] == "+12.5 pts", facts
            assert facts["banner"] is False and facts["alert"] is False, facts
            assert facts["hash"].startswith("#run="), facts
            assert facts["strayText"] is False, "page renders null/undefined/NaN text"

            # reload: terminal snapshot restored from #run=<id> without clicking anything
            await page.call("Page.reload")
            await page.wait_for("!!document.querySelector('[data-verdict]')")
            restored = await page.js(FACTS)
            assert restored["verdict"] == "verified" and restored["scores"] == ["62.5%", "75%"], (
                restored
            )
            assert restored["cards"] == facts["cards"] and restored["cta"] == "Run again", restored
            assert not page.errors, page.errors
            print("restored after reload: ok")
    finally:
        proc.terminate()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome")
    parser.add_argument("--shot", default="browser_smoke.png")
    args = parser.parse_args()
    chrome, port = find_chrome(args.chrome), free_port()
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/serve_demo.py"), "--mock-models", "--port", str(port)],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise SystemExit("FAIL: server did not start")
        asyncio.run(drive(f"http://127.0.0.1:{port}", chrome, Path(args.shot)))
    finally:
        server.terminate()
    print("PASS: real browser drove the real API through the mock-model orchestration path")


if __name__ == "__main__":
    main()
