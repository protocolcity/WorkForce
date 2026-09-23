"""Check distributable source without printing private matched content.

Synthetic tests are excluded; operational documents and product code are checked.
This guard detects common mistakes, not every secret or every private statement.
"""
from pathlib import Path
import re
import subprocess
import sys

RULES = {
    'personal absolute path': re.compile(r'/Users/[A-Za-z0-9_.-]+/|/home/[A-Za-z0-9_.-]+/'),
    'private key': re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----'),
    'credential shape': re.compile(r'\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|sk_live_[A-Za-z0-9]{16,})'),
}
TEXT = {'.md', '.py', '.sh', '.json', '.toml', '.yaml', '.yml', '.txt'}


def scan(root):
    raw = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z'])
    findings = []
    for name in raw.decode().split('\0'):
        if not name:
            continue
        path = root / name
        if path.suffix in ('.db', '.sqlite', '.sqlite3') or path.name == '.env':
            findings.append((name, 'private runtime file'))
        if name.startswith(('tests/', '.git/')) or path.suffix not in TEXT or not path.is_file():
            continue
        value = path.read_text(encoding='utf-8', errors='replace')
        findings.extend((name, label) for label, pattern in RULES.items() if pattern.search(value))
    return findings


if __name__ == '__main__':
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    findings = scan(root)
    for path, rule in findings:
        print('%s: %s' % (path, rule))
    print('Public source: %d finding(s)' % len(findings))
    raise SystemExit(bool(findings))
