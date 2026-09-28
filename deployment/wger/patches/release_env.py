import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit

HOST_NAME = re.compile(r'(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*')


def public_origin(value: str) -> str:
    """Return value as a bare http(s)://host[:port] origin, or raise ValueError.

    The release first uses this origin after it has stopped writers and migrated,
    so anything http.client/urllib would reject later must be refused here.
    """
    if (not value.isascii() or not value.isprintable() or ' ' in value
            or any(char in value for char in '@?#\\%') or 'REQUIRED_' in value):
        raise ValueError('not an origin')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError('not an origin') from None
    host = parts.hostname or ''
    if ':' in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError('not an origin') from None
    elif not HOST_NAME.fullmatch(host):
        raise ValueError('not an origin')
    if (parts.scheme not in {'http', 'https'} or port == 0 or parts.netloc.endswith(':')
            or parts.path not in {'', '/'}):
        raise ValueError('not an origin')
    return f'{parts.scheme}://{parts.netloc}'


def resolve_public_origin(configured: str | None, path: Path) -> str:
    """WGER_PUBLIC_URL when set and non-empty, else the single SITE_URL in private.env.

    An invalid override is refused, never replaced by the fallback.
    """
    if configured:
        source, value = 'WGER_PUBLIC_URL', configured
    else:
        # Value kept verbatim: trailing whitespace or comments are refused, not trimmed.
        values = [line.lstrip().split('=', 1)[1] for line in path.read_text().splitlines()
                  if line.lstrip().startswith('SITE_URL=')]
        if len(values) != 1:
            raise ValueError('WGER_PUBLIC_URL is unset and private.env must set SITE_URL exactly once')
        source, value = 'private.env SITE_URL', values[0]
    try:
        return public_origin(value)
    except ValueError:
        raise ValueError(f'{source} must name the gym public origin as http(s)://host[:port]') from None


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
