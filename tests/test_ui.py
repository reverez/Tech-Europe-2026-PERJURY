import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from perjury.api import app

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)


def test_demo_shell_is_served_same_origin_without_external_assets() -> None:
    html = client.get("/demo")
    assert html.status_code == 200
    assert 'id="cta"' in html.text
    assert "http://" not in html.text and "https://" not in html.text


@pytest.mark.parametrize("asset", ["app.js", "render.js", "store.js", "adapters.js", "styles.css"])
def test_ui_assets_are_served_from_package(asset: str) -> None:
    assert client.get(f"/ui/{asset}").status_code == 200


def test_ui_never_sets_html_from_data() -> None:
    for js in (ROOT / "perjury" / "ui").glob("*.js"):
        assert "innerHTML" not in js.read_text(), js.name


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ui_store_unit_tests() -> None:
    r = subprocess.run(
        ["node", "--test", "tests/ui/store.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
