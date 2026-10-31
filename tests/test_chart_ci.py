"""Chart CI and publishing (D17, D28)."""

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/chart-version.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _ci() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text())


def _chart_version() -> str:
    chart = yaml.safe_load((ROOT / "chart/agents-orchestrator/Chart.yaml").read_text())
    return chart["version"]


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, cwd=ROOT)


def _runs(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in job["steps"])


def test_chart_job_on_every_push():
    ci = _ci()
    on = ci.get("on", ci.get(True))
    assert "pull_request" in on
    assert "main" in on["push"]["branches"]
    job = ci["jobs"]["chart"]
    assert "if" not in job
    runs = _runs(job)
    assert "mise run lint:chart" in runs
    assert "uv run pytest tests/test_chart_*.py -q" in runs
    assert any("mise-action" in s.get("uses", "") for s in job["steps"])


GATE = {"lint", "test", "e2e"}


def test_image_push_needs_lint_test_e2e():
    job = _ci()["jobs"]["images"]
    assert set(job["needs"]) >= GATE
    pushing = [s for s in job["steps"] if "regctl image import" in s.get("run", "")]
    assert len(pushing) == 1
    assert pushing[0]["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    for step in job["steps"]:
        if "registry login" in step.get("run", ""):
            assert step["if"] == pushing[0]["if"]


def test_e2e_does_not_need_pushed_image():
    jobs = _ci()["jobs"]
    assert "images" not in jobs["e2e"]["needs"]
    assert "containers/agents-orchestrator" not in str(jobs["e2e"])
    script = (ROOT / "scripts/e2e.sh").read_text()
    assert "ORCH_IMAGE=agents-orchestrator:e2e" in script
    assert 'docker build -t "$ORCH_IMAGE" .' in script


def test_publish_only_main_after_images():
    main = _ci()["jobs"]["chart-publish"]
    assert set(main["needs"]) == {"chart", "images"} | GATE
    assert main["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    runs = _runs(main)
    assert 'scripts/chart-version.sh branch "$GITHUB_REF_NAME" "$GITHUB_SHA"' in runs
    assert main["env"]["CHART_REPO"] == "oci://harbor.szymonrichert.pl/charts"
    assert 'helm push "agents-orchestrator-$VERSION.tgz" "$CHART_REPO"' in runs
    assert "regctl image digest" not in runs


def test_no_tag_trigger_or_tag_publish_job():
    ci = _ci()
    on = ci.get("on", ci.get(True))
    assert "tags" not in on["push"]
    assert "chart-publish-tag" not in ci["jobs"]
    text = (ROOT / ".github/workflows/ci.yaml").read_text()
    assert "startsWith(github.ref, 'refs/tags" not in text
    tag = _run("tag", f"v{_chart_version()}", SHA)
    assert tag.returncode == 2 and tag.stdout == ""


def test_chart_version_scheme():
    v = _chart_version()
    main = _run("branch", "main", SHA)
    assert main.returncode == 0 and main.stdout.strip() == f"{v}-main.{SHA[:7]}"
    for ref in (f"v{v}", "v9.9.9"):
        tag = _run("tag", ref, SHA)
        assert tag.returncode == 2 and tag.stdout == "", ref
    assert _run("branch", "feature", SHA).returncode == 2
    assert _run("pull_request", "1/merge", SHA).returncode == 2
    assert _run("tag", "nov1.0.0", SHA).returncode == 2


def test_publish_refuses_existing_version():
    steps = _ci()["jobs"]["chart-publish"]["steps"]
    runs = [s.get("run", "") for s in steps]
    check = next(i for i, r in enumerate(runs) if "helm show chart" in r)
    push = next(i for i, r in enumerate(runs) if "helm push" in r)
    assert check < push
    assert '"$CHART_REPO/agents-orchestrator"' in runs[check]
    assert "--version" in runs[check]
    assert "already published; bump chart/agents-orchestrator/Chart.yaml version" in runs[check]
    assert "exit 1" in runs[check]


def test_package_sets_app_version_sha7():
    job = _ci()["jobs"]["chart-publish"]
    cmd = 'helm package chart/agents-orchestrator --version "$VERSION" --app-version "$SHA7"'
    assert cmd in _runs(job)
    assert "HARBOR_USERNAME" in str(job)
    assert "HARBOR_PASSWORD" in str(job)


def test_readme_documents_releasing():
    text = (ROOT / "README.md").read_text()
    assert "## Releasing" in text
    section = text.split("## Releasing")[1]
    assert "-main." in section and "oci://harbor.szymonrichert.pl/charts" in section
    assert "Chart.yaml" in section
    assert "`lint`, `test` and `e2e`" in section
    assert "never pushed" in section
    for phrase in (
        "conventional commits",
        "`vX.Y.Z`",
        "`0.1.0`",
        "`X.Y.Z`, `X.Y` and `X`",
        "`release` job",
        "pin `X.Y.Z`",
    ):
        assert phrase in section, phrase
    assert "A tag `vX.Y.Z`" not in section
    assert "git tag vX.Y.Z" not in section
    assert "publishes `X.Y.Z`" not in section


def test_e2e_job_runs_wrapper_contract_against_its_agent_image():
    """AC4 evidence path: the real-wrapper leg runs in CI on the image scripts/e2e.sh builds."""
    job = _ci()["jobs"]["e2e"]
    steps = job["steps"]
    kind = next(i for i, s in enumerate(steps) if s.get("run") == "mise run test:e2e")
    integ = [i for i, s in enumerate(steps) if s.get("run") == "mise run test:integration"]
    assert len(integ) == 1 and integ[0] > kind
    step = steps[integ[0]]
    assert "if" not in step
    script = (ROOT / "scripts/e2e.sh").read_text()
    image = next(
        line.split("=", 1)[1] for line in script.splitlines() if line.startswith("AGENT_IMAGE=")
    )
    assert step["env"]["ORCH_TEST_WRAPPER_IMAGE"] == image
    assert {"lint", "test", "e2e"} <= set(_ci()["jobs"]["images"]["needs"])
    assert "e2e" in _ci()["jobs"]["chart-publish"]["needs"]


def test_readme_documents_integration_in_ci():
    text = (ROOT / "README.md").read_text()
    dev = text.split("## Development")[1].split("## Configuration")[0]
    assert "`e2e` job" in dev and "mise run test:integration" in dev
