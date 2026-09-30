from workflow.cli import main


def _base(ws):
    return ["--runs-dir", str(ws["runs"]), "--models-dir", str(ws["models"])]


def test_cli_run_approve_models_rollback(ws, capsys):
    base = _base(ws)
    assert main(base + ["run", "--rehearsal", "--train", str(ws["train"]), "--holdout", str(ws["holdout"])]) == 0
    out = capsys.readouterr().out
    assert "[awaiting_approval]" in out and "불통과 (학습용에서만 효과, 과적합)" in out and "[2] 통과" in out
    [run_dir] = list(ws["runs"].iterdir())

    assert main(base + ["approve", run_dir.name, "--proposal", "1"]) == 2
    assert "--override-verdict" in capsys.readouterr().err
    assert main(base + ["approve", run_dir.name]) == 0
    assert "rule@v1 → rule@v2" in capsys.readouterr().out

    assert main(base + ["models"]) == 0
    assert "* rule@v2  부모 v1" in capsys.readouterr().out
    assert main(base + ["rollback", "--reason", "x"]) == 0
    assert "rule@v2 → rule@v1" in capsys.readouterr().out
    assert main(base + ["rollback", "--reason", "x"]) == 2
    assert main(base + ["approve", "nope"]) == 2


def test_override_requires_reason(ws, capsys):
    try:
        main(_base(ws) + ["approve", "x", "--override-verdict"])
    except SystemExit as exc:
        assert exc.code == 2
    assert "--reason" in capsys.readouterr().err


def test_cli_reports_unexpected_failure_without_traceback(ws, capsys):
    ws["train"].write_text("cases: [", encoding="utf-8")
    code = main(_base(ws) + ["run", "--rehearsal", "--train", str(ws["train"]), "--holdout", str(ws["holdout"])])
    err = capsys.readouterr().err
    assert code == 1 and "실행 실패" in err and "기록:" in err
