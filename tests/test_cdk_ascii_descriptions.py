"""Guard against IAM description validation failures on CDK deploy.

IAM rejects resource descriptions containing characters outside the regex
``[\\u0009\\u000A\\u000D\\u0020-\\u007E\\u00A1-\\u00FF]`` (Latin-1 printable
plus tab/LF/CR). Common offenders are UTF-8 smart punctuation (em dash,
curly quotes, ellipsis, arrows). We keep all Python source under
``infra/cdk`` ASCII-only so nothing slips through into an IAM description.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CDK_DIR = REPO_ROOT / "infra" / "cdk"

# IAM description allow-list (as bytes, per AWS error message).
IAM_VALID = re.compile(rb"^[\x09\x0A\x0D\x20-\x7E\xA1-\xFF]*$")


def test_cdk_python_sources_are_ascii_only():
    offenders: list[tuple[str, int, int]] = []
    for py in CDK_DIR.rglob("*.py"):
        data = py.read_bytes()
        for i, b in enumerate(data):
            if b > 0x7E and b < 0xA1:
                line_no = data[:i].count(b"\n") + 1
                offenders.append((str(py.relative_to(REPO_ROOT)), line_no, b))
                break
            if b > 0x7F:
                # UTF-8 multibyte sequence: any such byte lands outside
                # the IAM regex once the full codepoint is considered.
                line_no = data[:i].count(b"\n") + 1
                offenders.append((str(py.relative_to(REPO_ROOT)), line_no, b))
                break
    assert not offenders, (
        "Non-ASCII bytes found in CDK Python sources. Replace smart "
        "punctuation (em dash, curly quotes, etc.) with plain ASCII to "
        "avoid IAM description validation errors:\n"
        + "\n".join(f"  {p}:{ln} byte=0x{b:02x}" for p, ln, b in offenders)
    )
