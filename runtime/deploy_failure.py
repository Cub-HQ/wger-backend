"""Safe, bounded failure diagnostics for deployment subprocess boundaries."""
import json
import re
import subprocess


def redact_message(message: str) -> str:
    message = re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"\'<>]+', '[REDACTED]', message)
    message = re.sub(r'(?i)\b(?:authorization\s*[:=]?\s*)?(?:bearer|basic)\s+\S+', '[REDACTED]', message)
    message = re.sub(r'''(?ix)(?<!\w)((?:--)?[\w-]*(?:password|passwd|secret|token|api[_-]?key|authorization)[\w-]*["']?)(\s*[:=]\s*|\s+)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1\2[REDACTED]', message)
    return ' '.join(message.split())[:1000]


def _captured_output(error):
    for name in ('stderr', 'stdout'):
        value = getattr(error, name, None)
        if isinstance(value, bytes):
            value = value.decode('utf-8', errors='replace')
        if isinstance(value, str) and value.strip():
            yield value


def _receipt(output: str):
    try:
        receipt = json.loads(output)
    except ValueError:
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines:
            return None
        try:
            receipt = json.loads(lines[-1])
        except ValueError:
            return None
    if not isinstance(receipt, dict) or not isinstance(receipt.get('status'), str):
        return None
    reason = receipt.get('reason')
    if isinstance(reason, str) and reason.strip():
        return receipt
    error = receipt.get('error')
    if receipt['status'] == 'retryable' and isinstance(error, str) and error.strip():
        return receipt
    return None


def failure_receipt(error):
    """Select a controlled whole or terminal receipt, preferring stderr."""
    for output in _captured_output(error):
        receipt = _receipt(output)
        if receipt is not None:
            return receipt
    return None


def _safe_unstructured(error) -> str:
    """Extract only recognized operating-system causes from unstructured output."""
    causes = (
        r'No space left on device',
        r'Permission denied',
        r'Read-only file system',
        r'Connection (?:refused|reset|timed out)',
        r'Operation timed out',
        r'Network is unreachable',
        r'Host is unreachable',
        r'No such file or directory',
    )
    pattern = re.compile('|'.join(f'(?:{cause})' for cause in causes), re.IGNORECASE)
    for output in _captured_output(error):
        match = pattern.search(output)
        if match:
            return match.group(0)
    return ''

def failure_reason(error) -> str:
    receipt = failure_receipt(error)
    if receipt is not None:
        message = receipt.get('reason', receipt.get('error', ''))
    else:
        message = _safe_unstructured(error)
    if not message.strip():
        # Never stringify subprocess exceptions: their rendering includes argv.
        if isinstance(error, subprocess.CalledProcessError):
            message = f'command failed with exit status {error.returncode}'
        elif isinstance(error, subprocess.TimeoutExpired):
            message = f'command timed out after {error.timeout} seconds'
        elif isinstance(error, subprocess.SubprocessError):
            message = 'subprocess failed'
        else:
            message = str(error) or 'operation failed'
    return f'{type(error).__name__}: {redact_message(message) or "operation failed"}'
