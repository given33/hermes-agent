from pathlib import Path


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yaml"


def test_all_checks_gate_uses_independent_docker_workflow_and_rejects_nonterminal_results():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "      - docker\n" not in workflow
    assert "The image build runs in its own workflow (docker.yml)" in workflow
    # The upstream orchestrator now treats only an explicit failure as a gate
    # failure; skipped lane workflows are valid when the change classifier
    # says they are irrelevant.
    assert "info['result'] == 'failure'" in workflow
    assert "All checks passed (or were skipped)" in workflow


def test_production_deploy_waits_for_the_same_commit_ci_run():
    workflow = WORKFLOW.parent / "deploy-three-endpoints.yml"
    source = workflow.read_text(encoding="utf-8")
    ci_source = WORKFLOW.read_text(encoding="utf-8")

    assert "Wait for the complete CI gate" in source
    assert "--workflow ci.yaml" in source
    assert "--commit \"$RELEASE_COMMIT\"" in source
    assert "Timed out waiting for the complete CI gate" in source
    assert "--extra all --extra dev --extra hindsight" in source
    # Release events are intentionally owned by the production deployment
    # workflow.  CI remains the pull-request/main push gate and deployment
    # waits on the same commit's CI run above.
    assert "  pull_request:\n  push:\n    branches: [main]\n" in ci_source
