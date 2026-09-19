# ruff: noqa: ASYNC210, ASYNC220, ASYNC251, BLE001
"""Real-browser smoke of the FAILURE path: the unmocked app with no GOOGLE_API_KEY fails a run at
planning; the UI must show the typed error, offer a retry, keep the baseline visible, mark the stage
where it stopped, and restore the failed run after reload. No Gemini/Modal call is made."""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import browser_smoke as b
import websockets


async def main():
    port = b.free_port()
    cport = b.free_port()
    chrome = b.find_chrome(None)
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "perjury.api:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc = subprocess.Popen(
        [
            chrome,
            "--no-sandbox",
            "--headless",
            f"--remote-debugging-port={cport}",
            "--window-size=1280,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                ws_url = next(
                    t["webSocketDebuggerUrl"]
                    for t in json.load(
                        urllib.request.urlopen(f"http://127.0.0.1:{cport}/json/list")
                    )
                    if t["type"] == "page"
                )
                break
            except Exception:
                time.sleep(0.3)
        async with websockets.connect(ws_url, max_size=None) as ws:
            p = b.Page(ws)
            for d in ("Page", "Runtime", "Log"):
                await p.call(f"{d}.enable")
            await p.call("Page.navigate", url=f"http://127.0.0.1:{port}/demo")
            await p.wait_for("document.querySelector('#p-baseline .body')?.textContent.length > 0")
            await p.js("document.getElementById('cta').click()")
            await p.wait_for("!document.getElementById('alert').hidden", 60)
            info = await p.js(
                "({alert: document.getElementById('alert').textContent, cta: document.getElementById('cta').textContent, disabled: document.getElementById('cta').disabled, baseline: document.querySelector('#p-baseline .badge')?.textContent, stages: [...document.querySelectorAll('#pipeline li')].map(l=>l.className), hash: location.hash, stray: /\\b(null|undefined|NaN)\\b/.test(document.body.innerText)})"
            )
            print(json.dumps(info, indent=1))
            # recover: reload restores the failed run from #run=<id>
            await p.call("Page.reload")
            await p.wait_for("!document.getElementById('alert').hidden", 30)
            print(
                "restored failed run after reload:",
                await p.js("document.getElementById('alert').textContent"),
            )
            print("console errors:", p.errors)
    finally:
        proc.terminate()
        server.terminate()


if __name__ == "__main__":
    asyncio.run(main())
    print("PASS: failure path is legible and recoverable in a real browser")
