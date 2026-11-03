import json
import stat
import pytest
from scripts.sso_login import callback_code, save_private, api_origin


def test_callback_rejects_wrong_state_duplicate_parameters_and_wrong_path():
    state, code = "expected-state", "c" * 43
    assert callback_code(f"/callback?state={state}&code={code}", state) == code
    for path in (
        f"/callback?state=wrong&code={code}",
        f"/callback?state=%C3%A9&code={code}",
        f"/callback?state={state}&state={state}&code={code}",
        f"/callback?state={state}&code={code}&code={code}",
        f"/other?state={state}&code={code}",
    ):
        with pytest.raises(ValueError):
            callback_code(path, state)


def test_credential_file_is_private_and_never_overwritten(tmp_path):
    path = tmp_path / "credentials.json"
    save_private(path, {"access_token": "private-token"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"access_token": "private-token"}
    with pytest.raises(FileExistsError):
        save_private(path, {"access_token": "replacement"})
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        save_private(link, {})


def test_cli_requires_https_except_local_development():
    assert api_origin("https://vpn.example/") == "https://vpn.example"
    assert api_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    for origin in (
        "http://vpn.example",
        "https://name:secret@vpn.example",
        "https://vpn.example/path",
    ):
        with pytest.raises(ValueError):
            api_origin(origin)
