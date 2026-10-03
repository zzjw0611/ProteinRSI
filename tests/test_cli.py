import json

import pytest

from proteinrsi.cli import main


def test_cli_demo_and_status(tmp_path, capsys):
    path = tmp_path / "demo"
    main(["demo", "--out", str(path), "--rounds", "3"])
    report = json.loads(capsys.readouterr().out)
    assert report["evidence_source"] == "synthetic"
    assert report["completed_rounds"] == 3
    assert report["budget"]["llm_calls"]["committed"] == 0
    main(["status", "--campaign", str(path / "campaign")])
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    with pytest.raises(SystemExit) as exc:
        main(["demo", "--out", str(path)])
    assert exc.value.code == 2
