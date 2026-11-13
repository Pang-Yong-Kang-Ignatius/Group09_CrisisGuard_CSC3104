import re

import pytest

from infrastructure.scripts import init_local_env as module


TOKEN_KEYS = module.REQUIRED_TOKENS


def example() -> str:
    return (
        "POSTGRES_USER=crisisguard\n"
        "CONTROLLER_CLOUD_TOKEN=\n"
        "HOSPITAL_A_CONTROLLER_TOKEN=\n"
        "HOSPITAL_B_CONTROLLER_TOKEN=\n"
        "CLOUD_LIVE_TELEMETRY_TOKEN=\n"
        "CLOUD_REPLAY_TELEMETRY_TOKEN=\n"
        "OTHER_SETTING=preserve-me\n"
    )


def values(path):
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def test_first_run_copies_example_and_generates_three_private_tokens(tmp_path):
    (tmp_path / ".env.example").write_text(example(), encoding="utf-8")

    assert module.initialize_environment(tmp_path) == (True, True)

    result = values(tmp_path / ".env")
    tokens = [result[key] for key in TOKEN_KEYS]
    assert all(re.fullmatch(r"[0-9a-f]{64}", token) for token in tokens)
    assert len(set(tokens)) == 3
    assert result["OTHER_SETTING"] == "preserve-me"
    assert result["CLOUD_LIVE_TELEMETRY_TOKEN"] == ""
    assert result["CLOUD_REPLAY_TELEMETRY_TOKEN"] == ""


def test_local_launcher_fresh_install_generates_telemetry_and_repeat_preserves_bytes(tmp_path):
    (tmp_path / ".env.example").write_text(example(), encoding="utf-8")

    assert module.initialize_environment(tmp_path, include_test_traffic=True) == (True, True)
    environment = tmp_path / ".env"
    result = values(environment)
    keys = TOKEN_KEYS + module.LOCAL_TEST_TRAFFIC_TOKENS
    assert all(re.fullmatch(r"[0-9a-f]{64}", result[key]) for key in keys)
    assert len({result[key] for key in keys}) == len(keys)
    assert result["OTHER_SETTING"] == "preserve-me"

    before = environment.read_bytes()
    assert module.initialize_environment(tmp_path, include_test_traffic=True) == (False, False)
    assert environment.read_bytes() == before


def test_local_launcher_fills_only_missing_telemetry_credential(tmp_path):
    content = example().replace("CLOUD_LIVE_TELEMETRY_TOKEN=\n",
                                "CLOUD_LIVE_TELEMETRY_TOKEN=" + "l" * 64 + "\n")
    (tmp_path / ".env").write_text(content, encoding="utf-8")
    assert module.initialize_environment(tmp_path, include_test_traffic=True) == (False, True)
    result = values(tmp_path / ".env")
    assert result["CLOUD_LIVE_TELEMETRY_TOKEN"] == "l" * 64
    assert len(result["CLOUD_REPLAY_TELEMETRY_TOKEN"]) == 64
    assert result["CLOUD_REPLAY_TELEMETRY_TOKEN"] != result["CLOUD_LIVE_TELEMETRY_TOKEN"]


@pytest.mark.parametrize("bad", ["short", "x" * 64])
def test_local_launcher_rejects_invalid_or_reused_telemetry_token(tmp_path, bad):
    content = example().replace("CLOUD_LIVE_TELEMETRY_TOKEN=\n",
                                f"CLOUD_LIVE_TELEMETRY_TOKEN={bad}\n")
    if len(bad) == 64:
        content = content.replace("CLOUD_REPLAY_TELEMETRY_TOKEN=\n",
                                  f"CLOUD_REPLAY_TELEMETRY_TOKEN={bad}\n")
    environment = tmp_path / ".env"
    environment.write_text(content, encoding="utf-8")
    before = environment.read_bytes()
    with pytest.raises(module.EnvironmentInitializationError):
        module.initialize_environment(tmp_path, include_test_traffic=True)
    assert environment.read_bytes() == before


def test_rerun_preserves_valid_tokens_and_file_bytes(tmp_path, monkeypatch):
    tokens = [
        "existing-controller-token-with-symbols!",
        "existing-hospital-a-token-with-symbols!",
        "existing-hospital-b-token-with-symbols!",
    ]
    content = example()
    for key, token in zip(TOKEN_KEYS, tokens):
        content = content.replace(f"{key}=\n", f"{key}={token}\n")
    environment = tmp_path / ".env"
    environment.write_text(content, encoding="utf-8")
    before = environment.read_bytes()
    monkeypatch.setattr(
        module.secrets,
        "token_hex",
        lambda _: pytest.fail("valid rerun must not generate tokens"),
    )

    assert module.initialize_environment(tmp_path) == (False, False)
    assert environment.read_bytes() == before


def test_partial_configuration_preserves_values_and_fills_only_missing(
    tmp_path, monkeypatch
):
    existing = "d" * 64
    content = (
        f"CONTROLLER_CLOUD_TOKEN={existing}\r\n"
        "HOSPITAL_A_CONTROLLER_TOKEN=\r\n"
        "CLOUD_LIVE_TELEMETRY_TOKEN=optional-live-value\r\n"
        "OTHER_SETTING=unchanged value\r\n"
    )
    (tmp_path / ".env").write_bytes(content.encode())
    generated = iter(("e" * 64, "f" * 64))
    calls = []

    def token_hex(size):
        calls.append(size)
        return next(generated)

    monkeypatch.setattr(module.secrets, "token_hex", token_hex)
    assert module.initialize_environment(tmp_path) == (False, True)

    result = values(tmp_path / ".env")
    assert result["CONTROLLER_CLOUD_TOKEN"] == existing
    assert result["HOSPITAL_A_CONTROLLER_TOKEN"] == "e" * 64
    assert result["HOSPITAL_B_CONTROLLER_TOKEN"] == "f" * 64
    assert result["CLOUD_LIVE_TELEMETRY_TOKEN"] == "optional-live-value"
    assert result["OTHER_SETTING"] == "unchanged value"
    assert calls == [32, 32]
    assert b"\r\n" in (tmp_path / ".env").read_bytes()


def test_generation_retries_a_collision_and_keeps_tokens_unique(tmp_path, monkeypatch):
    existing = "a" * 64
    (tmp_path / ".env").write_text(
        example().replace(
            "CONTROLLER_CLOUD_TOKEN=\n",
            f"CONTROLLER_CLOUD_TOKEN={existing}\n",
        ),
        encoding="utf-8",
    )
    generated = iter((existing, "b" * 64, "b" * 64, "c" * 64))
    monkeypatch.setattr(module.secrets, "token_hex", lambda size: next(generated))

    module.initialize_environment(tmp_path)

    result = values(tmp_path / ".env")
    assert {result[key] for key in TOKEN_KEYS} == {
        "a" * 64,
        "b" * 64,
        "c" * 64,
    }


@pytest.mark.parametrize(
    "content",
    [
        "CONTROLLER_CLOUD_TOKEN=short\n",
        "CONTROLLER_CLOUD_TOKEN=" + "a" * 64 + "\n"
        "HOSPITAL_A_CONTROLLER_TOKEN=" + "a" * 64 + "\n",
        "CONTROLLER_CLOUD_TOKEN=\nCONTROLLER_CLOUD_TOKEN=\n",
    ],
)
def test_invalid_configuration_fails_without_modifying_file(tmp_path, content):
    environment = tmp_path / ".env"
    environment.write_text(content, encoding="utf-8")
    before = environment.read_bytes()

    with pytest.raises(module.EnvironmentInitializationError):
        module.initialize_environment(tmp_path)

    assert environment.read_bytes() == before
    assert not list(tmp_path.glob(".env.*.tmp"))


def test_populated_example_is_rejected_without_creating_env(tmp_path):
    (tmp_path / ".env.example").write_text(
        example().replace(
            "CONTROLLER_CLOUD_TOKEN=\n",
            "CONTROLLER_CLOUD_TOKEN=" + "a" * 64 + "\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.EnvironmentInitializationError):
        module.initialize_environment(tmp_path)

    assert not (tmp_path / ".env").exists()


def test_atomic_replace_failure_preserves_existing_file(tmp_path, monkeypatch):
    environment = tmp_path / ".env"
    environment.write_text(example(), encoding="utf-8")
    before = environment.read_bytes()

    def fail_replace(*_):
        raise OSError("blocked")

    monkeypatch.setattr(module.os, "replace", fail_replace)

    with pytest.raises(module.EnvironmentInitializationError):
        module.initialize_environment(tmp_path)

    assert environment.read_bytes() == before
    assert not list(tmp_path.glob(".env.*.tmp"))


def test_launcher_uses_python_initializer_without_powershell():
    launcher = (
        module.Path(__file__).resolve().parents[2] / "start.project.bat"
    ).read_text()
    assert '"infrastructure\\scripts\\init_local_env.py"' in launcher
    assert "powershell" not in launcher.lower()
    assert '"infrastructure\\scripts\\init_local_env.py" --with-local-test-traffic' in launcher


@pytest.mark.parametrize("with_traffic", [False, True])
def test_cli_output_never_contains_generated_tokens(tmp_path, monkeypatch, capsys, with_traffic):
    (tmp_path / ".env.example").write_text(example(), encoding="utf-8")
    generated = iter((str(i) * 64 for i in range(1, 6)))
    monkeypatch.setattr(module.secrets, "token_hex", lambda size: next(generated))

    arguments = ["--root", str(tmp_path)]
    if with_traffic:
        arguments.append("--with-local-test-traffic")
    assert module.main(arguments) == 0

    output = capsys.readouterr()
    for token in (str(i) * 64 for i in range(1, 6)):
        assert token not in output.out
        assert token not in output.err
