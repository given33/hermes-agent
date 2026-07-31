from pathlib import Path


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"


def test_all_checks_gate_requires_docker_and_rejects_nonterminal_results():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "      - docker\n" in workflow
    assert "info['result'] not in ('success', 'skipped')" in workflow
    assert "required job(s) did not pass" in workflow


def test_production_deploy_waits_for_the_same_commit_ci_run():
    workflow = WORKFLOW.parent / "deploy-three-endpoints.yml"
    source = workflow.read_text(encoding="utf-8")
    ci_source = WORKFLOW.read_text(encoding="utf-8")

    assert "Wait for the complete CI gate" in source
    assert "--workflow ci.yml" in source
    assert "--commit \"$RELEASE_COMMIT\"" in source
    assert "Timed out waiting for the complete CI gate" in source
    assert "  release:\n    types: [published]\n" in ci_source
