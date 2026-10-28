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
    assert "v*" in on["push"]["tags"]
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


def test_publish_only_main_and_tags_after_images():
    jobs = _ci()["jobs"]
    main = jobs["chart-publish"]
    tag = jobs["chart-publish-tag"]
    assert set(main["needs"]) == {"chart", "images"} | GATE
    assert main["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert set(tag["needs"]) == {"chart"} | GATE
    assert tag["if"] == "startsWith(github.ref, 'refs/tags/v')"
    for job in (main, tag):
        runs = _runs(job)
        assert "scripts/chart-version.sh" in runs
        assert job["env"]["CHART_REPO"] == "oci://harbor.szymonrichert.pl/charts"
        assert 'helm push "agents-orchestrator-$VERSION.tgz" "$CHART_REPO"' in runs
    assert "regctl image digest" in _runs(tag)
    assert "harbor.szymonrichert.pl/containers/agents-orchestrator:$SHA7" in _runs(tag)
    assert "regctl image digest" not in _runs(main)


def test_chart_version_scheme():
    v = _chart_version()
    main = _run("branch", "main", SHA)
    assert main.returncode == 0 and main.stdout.strip() == f"{v}-main.{SHA[:7]}"
    tag = _run("tag", f"v{v}", SHA)
    assert tag.returncode == 0 and tag.stdout.strip() == v
    bad = _run("tag", "v9.9.9", SHA)
    assert bad.returncode == 1
    assert "9.9.9" in bad.stderr and v in bad.stderr
    assert _run("branch", "feature", SHA).returncode == 2
    assert _run("pull_request", "1/merge", SHA).returncode == 2
    assert _run("tag", "nov1.0.0", SHA).returncode == 2


def test_publish_refuses_existing_version():
    for name in ("chart-publish", "chart-publish-tag"):
        steps = _ci()["jobs"][name]["steps"]
        runs = [s.get("run", "") for s in steps]
        check = next(i for i, r in enumerate(runs) if "helm show chart" in r)
        push = next(i for i, r in enumerate(runs) if "helm push" in r)
        assert check < push
        assert '"$CHART_REPO/agents-orchestrator"' in runs[check]
        assert "--version" in runs[check]
        assert "already published; bump chart/agents-orchestrator/Chart.yaml version" in runs[check]
        assert "exit 1" in runs[check]


def test_package_sets_app_version_sha7():
    for name in ("chart-publish", "chart-publish-tag"):
        runs = _runs(_ci()["jobs"][name])
        cmd = 'helm package chart/agents-orchestrator --version "$VERSION" --app-version "$SHA7"'
        assert cmd in runs
        assert "HARBOR_USERNAME" in str(_ci()["jobs"][name])
        assert "HARBOR_PASSWORD" in str(_ci()["jobs"][name])


def test_readme_documents_releasing():
    text = (ROOT / "README.md").read_text()
    assert "## Releasing" in text
    section = text.split("## Releasing")[1]
    assert "-main." in section and "oci://harbor.szymonrichert.pl/charts" in section
    assert "Chart.yaml" in section
    assert "`lint`, `test` and `e2e`" in section
    assert "never pushed" in section


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
    for name in ("chart-publish", "chart-publish-tag"):
        assert "e2e" in _ci()["jobs"][name]["needs"]


def test_readme_documents_integration_in_ci():
    text = (ROOT / "README.md").read_text()
    dev = text.split("## Development")[1].split("## Configuration")[0]
    assert "`e2e` job" in dev and "mise run test:integration" in dev
