"""Initialize the local .env without exposing or rotating valid credentials."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile


REQUIRED_TOKENS = (
    "CONTROLLER_CLOUD_TOKEN",
    "HOSPITAL_A_CONTROLLER_TOKEN",
    "HOSPITAL_B_CONTROLLER_TOKEN",
)
LOCAL_TEST_TRAFFIC_TOKENS = (
    "CLOUD_LIVE_TELEMETRY_TOKEN",
    "CLOUD_REPLAY_TELEMETRY_TOKEN",
)
MINIMUM_EXISTING_TOKEN_LENGTH = 32
ASSIGNMENT_PATTERN = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*)(?P<value>.*)$"
)


class EnvironmentInitializationError(RuntimeError):
    """Safe configuration error whose message never contains a secret value."""


def _value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _assignments(lines: list[str], keys=REQUIRED_TOKENS) -> dict[str, tuple[int, str, str]]:
    assignments: dict[str, tuple[int, str, str]] = {}
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        match = ASSIGNMENT_PATTERN.fullmatch(content)
        if not match:
            continue
        key = match.group("key")
        if key not in keys:
            continue
        if key in assignments:
            raise EnvironmentInitializationError(
                f"Duplicate {key} definitions are not allowed"
            )
        assignments[key] = (index, match.group("prefix"), match.group("value"))
    return assignments


def _new_token(used: set[str]) -> str:
    while True:
        token = secrets.token_hex(32)
        if token not in used:
            return token


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def initialize_environment(root: Path, *, include_test_traffic=False) -> tuple[bool, bool]:
    """Return ``(created, changed)`` after safely preparing ``root/.env``."""
    root = root.resolve()
    environment = root / ".env"
    example = root / ".env.example"

    if environment.is_symlink():
        raise EnvironmentInitializationError("Refusing to replace a symlinked .env")

    created = not environment.exists()
    source = example if created else environment
    if not source.is_file():
        raise EnvironmentInitializationError(
            ".env.example is required when .env does not exist"
        )

    try:
        original = source.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise EnvironmentInitializationError("Unable to read local environment configuration") from error

    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    keys = REQUIRED_TOKENS + (LOCAL_TEST_TRAFFIC_TOKENS if include_test_traffic else ())
    assignments = _assignments(lines, keys)
    valid: dict[str, str] = {}
    missing: list[str] = []

    for key in keys:
        assignment = assignments.get(key)
        if assignment is None:
            missing.append(key)
            continue
        candidate = _value(assignment[2])
        if not candidate:
            missing.append(key)
        elif len(candidate) >= MINIMUM_EXISTING_TOKEN_LENGTH:
            valid[key] = candidate
        else:
            raise EnvironmentInitializationError(
                f"{key} must be empty or at least "
                f"{MINIMUM_EXISTING_TOKEN_LENGTH} characters"
            )

    if created and valid:
        raise EnvironmentInitializationError(
            ".env.example must not contain populated authentication tokens"
        )
    if len(set(valid.values())) != len(valid):
        raise EnvironmentInitializationError(
            "Local authentication tokens must be independent"
        )

    used = set(valid.values())
    replacements: dict[str, str] = {}
    for key in missing:
        token = _new_token(used)
        used.add(token)
        replacements[key] = token

    if not replacements and not created:
        return False, False

    for key, token in replacements.items():
        assignment = assignments.get(key)
        if assignment is None:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            lines.append(f"{key}={token}{newline}")
        else:
            index, prefix, _ = assignment
            ending = "\r\n" if lines[index].endswith("\r\n") else (
                "\n" if lines[index].endswith("\n") else ""
            )
            lines[index] = f"{prefix}{token}{ending}"

    try:
        _atomic_write(environment, "".join(lines))
    except OSError as error:
        raise EnvironmentInitializationError(
            "Unable to atomically update .env"
        ) from error
    return created, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--with-local-test-traffic",
        action="store_true",
        help="Generate distinct local telemetry credentials when missing",
    )
    args = parser.parse_args(argv)
    try:
        created, changed = initialize_environment(
            args.root, include_test_traffic=args.with_local_test_traffic
        )
    except EnvironmentInitializationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if created:
        print("Local .env initialized with private authentication tokens.")
    elif changed:
        print("Missing local authentication tokens generated safely.")
    else:
        print("Local authentication configuration is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
