"""The one-time codes that prove a GitHub link is a person's own (#195).

Shared by JotFlow, which issues a code and keeps only its digest, and the
drafting runner, which finds code-shaped words on GitHub and compares digests.
"""

from __future__ import annotations

import hashlib
import re
import secrets

CODE = re.compile(r"jotflow-[A-Za-z0-9_-]{12}")
# A code as a whole word in someone's bio or gist description.
CODE_WORD = re.compile(r"(?<![\w-])jotflow-[A-Za-z0-9_-]{12}(?![\w-])")
DIGEST = re.compile(r"[0-9a-f]{64}")


def new_code() -> str:
    """``jotflow-`` and 12 URL-safe characters, about 72 random bits."""
    return "jotflow-" + secrets.token_urlsafe(9)


def code_digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()
