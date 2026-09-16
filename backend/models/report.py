"""Models for Security and Validation Reports."""

from typing import List, Optional

from pydantic import BaseModel


class SecurityFinding(BaseModel):
    tool: str  # tfsec, checkov, trivy, conftest
    rule_id: str
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    description: str
    resource: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    remediation: Optional[str] = None


class SecurityReport(BaseModel):
    passed: bool
    risk_score: int  # 0 to 100
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    findings: List[SecurityFinding] = []


class ValidationCheck(BaseModel):
    check_name: str  # fmt, init, validate
    passed: bool
    output: Optional[str] = None
    errors: List[str] = []
    warnings: List[str] = []


class ValidationReport(BaseModel):
    passed: bool
    checks: List[ValidationCheck] = []
