import io

from unilabos.app.main import _first_run_confirmed


def test_first_run_prompt_defaults_to_yes_without_stdin(monkeypatch) -> None:
    # nohup / CI / 服务管理器下标准输入为空，input() 抛 EOFError，应按默认「是」继续。
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert _first_run_confirmed(False) is True


def test_first_run_prompt_accepts_enter_and_skips_in_check_mode(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert _first_run_confirmed(False) is True
    assert _first_run_confirmed(True) is True


def test_first_run_prompt_honours_explicit_no(monkeypatch) -> None:
    for answer in ("n\n", "N\n", " n \n"):
        monkeypatch.setattr("sys.stdin", io.StringIO(answer))
        assert _first_run_confirmed(False) is False
