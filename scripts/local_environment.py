"""Read probe settings from the project's local environment file."""

from pathlib import Path
import re


ENV_FILE = Path(__file__).resolve().parents[1] / ".env.local"
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_local_environment(path: Path = ENV_FILE) -> dict[str, str]:
    """Load local settings without falling back to another account's shell keys."""
    if not path.is_file():
        raise ValueError(".env.local is missing. Copy .env.example and supply the test keys.")

    settings = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue

        name, separator, raw_value = entry.removeprefix("export ").partition("=")
        name = name.strip()
        if not separator or not ENV_NAME.fullmatch(name):
            raise ValueError(f"Invalid setting on line {number} of .env.local.")

        value = raw_value.strip()
        if value.startswith(('"', "'")):
            value, closing_quote, trailing = value[1:].partition(value[0])
            if not closing_quote or (trailing.strip() and not trailing.lstrip().startswith("#")):
                raise ValueError(f"Invalid quoted value on line {number} of .env.local.")
        else:
            value = value.partition("#")[0].strip()
        settings[name] = value

    return settings
