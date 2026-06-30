"""
governance.py — Universal bedrock transit filter for PostCar network.

Every message/query passes through this layer before leaving the agent
and before being processed on receive. No agent bypasses this.

Standards basis: NIST AI RMF (GOVERN), NIST 800-53 (AU/PL/SI), ISO/IEC 27001.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# ── Classification levels ────────────────────────────────────────────────────

CLASSIFICATION_PUBLIC = "PUBLIC"
CLASSIFICATION_INTERNAL = "INTERNAL"
CLASSIFICATION_SENSITIVE = "SENSITIVE"
CLASSIFICATION_RESTRICTED = "RESTRICTED"

# ── PII patterns (NIST 800-53 PL-8, GDPR Art. 4) ────────────────────────────

_PII_PATTERNS: List[Tuple[str, re.Pattern, str]] = [
    ("email",       re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED:EMAIL]"),
    ("phone_us",    re.compile(r"\b(\+1[-.\s]?)?(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b"), "[REDACTED:PHONE]"),
    ("ssn",         re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED:CC]"),
    ("ip_address",  re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[REDACTED:IP]"),
    ("dob",         re.compile(r"\b(0[1-9]|1[0-2])[/\-](0[1-9]|[12]\d|3[01])[/\-](19|20)\d{2}\b"), "[REDACTED:DOB]"),
    ("passport",    re.compile(r"\b[A-Z]{1,2}\d{6,9}\b"), "[REDACTED:PASSPORT]"),
    ("iban",        re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b"), "[REDACTED:IBAN]"),
]

# ── Secret / credential patterns (NIST 800-53 IA-5, SI-12) ─────────────────

_SECRET_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("openai_key",    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("github_token",  re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github_token2", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    ("aws_key",       re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret",    re.compile(r"\b[0-9a-zA-Z/+]{40}\b(?=.*aws)", re.IGNORECASE)),
    ("bearer_token",  re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b")),
    ("private_key",   re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("generic_secret",re.compile(r"(?i)(secret|password|passwd|pwd|api[_\-]?key)\s*[:=]\s*\S{8,}")),
    ("jwt",           re.compile(r"\beyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\b")),
]

# ── Prompt injection patterns (NIST AI RMF GOVERN 1.6) ──────────────────────

_INJECTION_PATTERNS: List[re.Pattern] = [
    re.compile(r"(?i)ignore (all |previous |above |prior )?(instructions?|prompt|rules?|constraints?)"),
    re.compile(r"(?i)you are now (a |an )?(different|new|another|unrestricted|jailbreak)"),
    re.compile(r"(?i)(disregard|forget|override|bypass|circumvent).{0,40}(instructions?|rules?|guidelines?)"),
    re.compile(r"(?i)act as (if you (are|have) no|without) (restrictions?|limitations?|guidelines?)"),
    re.compile(r"(?i)(system prompt|system message|your instructions?)\s*[:=]"),
    re.compile(r"(?i)DAN mode|developer mode|jailbreak|do anything now"),
]

# ── Sensitivity keywords for classification ──────────────────────────────────

_SENSITIVE_KEYWORDS = [
    "confidential", "proprietary", "internal only", "not for distribution",
    "medical", "health record", "diagnosis", "prescription",
    "legal", "attorney", "privileged", "lawsuit",
    "salary", "compensation", "payroll",
]

_RESTRICTED_KEYWORDS = [
    "top secret", "classified", "restricted", "pii", "personally identifiable",
    "hipaa", "phi", "protected health",
    "financial statement", "audit report", "board minutes",
    "source code", "trade secret",
]


class GovernanceViolation(Exception):
    """Raised when content is blocked from transit."""
    def __init__(self, reason: str, finding_type: str):
        self.reason = reason
        self.finding_type = finding_type
        super().__init__(f"[governance:{finding_type}] {reason}")


def scrub_pii(text: str) -> Tuple[str, List[str]]:
    """Redact PII from text. Returns (scrubbed_text, list_of_findings)."""
    findings = []
    for name, pattern, replacement in _PII_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            findings.append(f"PII:{name} ({len(matches)} instance(s))")
            text = pattern.sub(replacement, text)
    return text, findings


def detect_secrets(text: str) -> List[str]:
    """Return list of secret type names found. Does NOT redact — BLOCKS transit."""
    found = []
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            found.append(name)
    return found


def detect_injection(text: str) -> List[str]:
    """Return list of injection pattern descriptions found."""
    found = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            found.append(pattern.pattern[:60])
    return found


def classify(text: str) -> str:
    """Classify text sensitivity level."""
    lower = text.lower()
    for kw in _RESTRICTED_KEYWORDS:
        if kw in lower:
            return CLASSIFICATION_RESTRICTED
    for kw in _SENSITIVE_KEYWORDS:
        if kw in lower:
            return CLASSIFICATION_SENSITIVE
    # PII presence → SENSITIVE minimum
    for _, pattern, _ in _PII_PATTERNS:
        if pattern.search(text):
            return CLASSIFICATION_SENSITIVE
    return CLASSIFICATION_PUBLIC


def _collect_strings(obj: Any) -> str:
    """Flatten all string values from a dict/list/str into one string for scanning."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(_collect_strings(v) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(_collect_strings(v) for v in obj)
    return str(obj) if obj is not None else ""


def apply(payload: Dict[str, Any], direction: str = "outbound") -> Dict[str, Any]:
    """
    Universal bedrock transit filter. Call before every send and on every receive.

    direction: "outbound" (before sending) | "inbound" (after receiving)

    Modifies payload in place (PII redacted).
    Raises GovernanceViolation if secrets or injection detected.
    Returns annotated payload with _governance metadata.
    """
    raw_text = _collect_strings(payload)

    # 1. Secret detection — hard block, do not transit
    secrets_found = detect_secrets(raw_text)
    if secrets_found:
        raise GovernanceViolation(
            f"Credential/secret detected in {direction} payload: {', '.join(secrets_found)}. "
            "Remove secrets before transiting the PostCar network.",
            finding_type="SECRET"
        )

    # 2. Prompt injection guard — hard block outbound, warn inbound
    injections = detect_injection(raw_text)
    if injections and direction == "outbound":
        raise GovernanceViolation(
            f"Prompt injection pattern detected in outbound payload.",
            finding_type="INJECTION"
        )

    # 3. PII scrub — redact and annotate (does not block)
    pii_findings: List[str] = []
    for key in list(payload.keys()):
        if isinstance(payload[key], str):
            scrubbed, found = scrub_pii(payload[key])
            payload[key] = scrubbed
            pii_findings.extend(found)

    # 4. Classify after scrub
    scrubbed_text = _collect_strings(payload)
    classification = classify(scrubbed_text)

    if classification == CLASSIFICATION_RESTRICTED:
        raise GovernanceViolation(
            f"Payload classified RESTRICTED — cannot transit PostCar network.",
            finding_type="RESTRICTED"
        )

    # 5. Attach governance metadata (non-blocking audit trail)
    payload["_governance"] = {
        "classification": classification,
        "pii_redacted": pii_findings,
        "injection_flags": injections if injections else [],
        "direction": direction,
        "passed": True,
    }

    return payload
