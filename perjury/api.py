from fastapi import FastAPI

from perjury.ui import mount_ui

app = FastAPI(
    title="PERJURY",
    description="Autonomous adversarial test-hardening agent",
    version="0.1.0",
)


mount_ui(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "perjury"}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "PERJURY",
        "tagline": "Your CI is green. PERJURY finds what your tests never proved.",
    }
