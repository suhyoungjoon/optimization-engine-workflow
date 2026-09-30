import yaml

from workflow.cli import main


def test_cli_run_status_approve(ws, capsys):
    base = ["--runs-dir", str(ws["runs"])]
    assert main(base + ["run", "--rehearsal", "--params", str(ws["params"]), "--faults", "P4"]) == 0
    out = capsys.readouterr().out
    assert "[awaiting_approval]" in out and "rule@v1" in out
    [run_dir] = list(ws["runs"].iterdir())

    assert main(base + ["status"]) == 0
    assert "awaiting_approval" in capsys.readouterr().out

    assert main(base + ["approve", run_dir.name, "--proposal", "2"]) == 2   # invalid 안
    assert "검증되지 않았다" in capsys.readouterr().err
    assert main(base + ["approve", run_dir.name]) == 0
    assert "rule@v1 → rule@v2" in capsys.readouterr().out
    assert yaml.safe_load(ws["params"].read_text(encoding="utf-8"))["version"] == 2

    assert main(base + ["reject", run_dir.name, "--reason", "x"]) == 2
    assert main(base + ["approve", "nope"]) == 2


def test_cli_reports_unexpected_failure_without_traceback(ws, capsys):
    ws["params"].write_text("version: [", encoding="utf-8")
    code = main(["--runs-dir", str(ws["runs"]), "run", "--rehearsal", "--params", str(ws["params"])])
    err = capsys.readouterr().err
    assert code == 1 and "실행 실패" in err and "기록:" in err
