"""This station's own values, from station.toml beside this file; station.example.toml lists them."""
import tomllib
from pathlib import Path

PATH = Path(__file__).with_name('station.toml')


def read(path=PATH):
    """The file's tables, and why it could not be read ('' when it was)."""
    try:
        return tomllib.loads(path.read_text()), ''
    except FileNotFoundError:
        return {}, f'{path.name} is missing; copy station.example.toml to {path} and fill it in'
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return {}, f'{path} could not be read: {exc}'


def get(section, key, path=PATH):
    """(value, '') for [section] key, or ('', why it is not set)."""
    tables, why = read(path)
    if why:
        return '', why
    table = tables.get(section)
    value = table.get(key) if isinstance(table, dict) else None
    if not isinstance(value, str) or not value.strip():
        return '', f'{path.name} has no [{section}] {key}'
    return value.strip(), ''
