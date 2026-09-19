from fastapi import FastAPI

app = FastAPI(
    title="PERJURY",
    description="Autonomous adversarial test-hardening agent",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "perjury"}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "PERJURY",
        "tagline": "Your CI is green. PERJURY finds what your tests never proved.",
    }
