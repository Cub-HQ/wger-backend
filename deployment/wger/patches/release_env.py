from pathlib import Path


def read_database_environment(path: Path) -> dict[str, str]:
    """Read only the two non-secret database identity fields from a Docker env file."""
    wanted = {'POSTGRES_USER', 'POSTGRES_DB'}
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key in wanted:
            values[key] = value
    missing = wanted - values.keys()
    if missing or any(not values[key] for key in wanted):
        raise ValueError('private.env must provide POSTGRES_USER and POSTGRES_DB')
    return values
