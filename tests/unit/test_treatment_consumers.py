"""Exercise treatment consumers through their normal install/upload interfaces."""
import json
from unittest.mock import Mock

import pytest
from study.eidos_arms import charters, common, protocols
from study.exp.treatment import resolve

from eidos.cli import main as cli


@pytest.mark.parametrize("selection", ["pilot4-v1", "v2-portfolio"])
@pytest.mark.parametrize("card", ["N01", "N02", "D03"])
def test_prepare_installs_the_resolved_component_plan(tmp_path, monkeypatch, selection, card):
    from eidos.tasks import bundle

    monkeypatch.setenv("EIDOS_STUDY_TREATMENT", selection)
    monkeypatch.setenv("EIDOS_RESEARCH_PYTHON", "fixture-python")
    treatment = resolve()
    (tmp_path / "arm.json").write_text(json.dumps({"treatment": treatment.record()}))
    client = Mock()
    client.query.return_value = {"body": {"policy": {"llm_broker": {"enabled": False}}}}
    monkeypatch.setattr(bundle, "import_bundle", lambda *a, **kw: {"task_id": card})
    monkeypatch.setattr(charters, "commit", lambda *a: {})
    monkeypatch.setattr(protocols, "prepare", lambda *a: None)
    installed = []

    def install(c, project, path, env, bind, *, upload_name=None):
        installed.append((path, bind, upload_name))
        return {"component_ref": path.name}

    monkeypatch.setattr(cli, "install_component", install)
    monkeypatch.setattr(common, "install_shared_project_command", lambda *a: installed.append((common.PROJECT_COMMAND_SOURCE, True, common.PROJECT_COMMAND_SOURCE.name)))
    common.prepare(client, tmp_path, 4)
    assert installed == [(path, bind, treatment.upload_name(path)) for path, bind in treatment.component_plan(card)]
    assert json.loads((tmp_path / "lab_import.json").read_text())["treatment"] == treatment.record()


@pytest.mark.parametrize("upload_name", [None, "eidos.diagnose.baseline-errors"])
def test_component_upload_name_preserves_legacy_default(tmp_path, monkeypatch, upload_name):
    client = Mock()
    client.upload.return_value = {"result": {"artifact_id": "art"}}
    client.command.return_value = {"result": {"job_id": "job", "component_ref": "component@1"}}
    monkeypatch.setattr(cli, "wait_job", lambda *a: {"id": "job", "status": "succeeded"})
    cli.install_component(client, "project", tmp_path, upload_name=upload_name)
    assert client.upload.call_args.kwargs["name"] == (tmp_path.name if upload_name is None else upload_name)
