"""Safety: policy, the confirmation gate, secret redaction, global abort."""
from __future__ import annotations

from .emergency import EmergencySwitch, emergency
from .guard import ActionGuard, ActionPolicy, Risk, describe, require_confirmation
from .policy import PolicyDecision, RiskClassifier, classify_risk
from .secrets import SecretStore, redact, scrub_data

RiskLevel = Risk

__all__ = [
    "ActionGuard",
    "ActionPolicy",
    "EmergencySwitch",
    "PolicyDecision",
    "Risk",
    "RiskClassifier",
    "RiskLevel",
    "SecretStore",
    "classify_risk",
    "describe",
    "emergency",
    "redact",
    "require_confirmation",
    "scrub_data",
]
