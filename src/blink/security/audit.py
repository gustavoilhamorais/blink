"""Security audit utilities.

Verifies that all registered MCP tools have correct capability requirements
and that the policy is correctly enforced.  Can be run standalone (e.g. in
CI) or called programmatically as part of a health check.

Usage::

    from blink.security.audit import audit_tools
    from blink.mcp.server import MCPServer

    findings = await audit_tools(mcp_server)
    for f in findings:
        print(f.severity, f.tool, f.issue)

Exit codes when used as a script:
    0 — no findings at critical/warning level
    1 — one or more critical findings
    2 — one or more warnings (but no criticals)
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from blink.security.capabilities import TOOL_CAPABILITY_MAP, Capability

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class AuditFinding(BaseModel):
    """A single security audit finding.

    Attributes:
        severity: One of ``"info"``, ``"warning"``, or ``"critical"``.
        tool: The MCP tool name this finding relates to.
        issue: Human-readable description of the issue.
        recommendation: Suggested remediation.
    """

    severity: str  # info | warning | critical
    tool: str
    issue: str
    recommendation: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.tool}: {self.issue}"


# ---------------------------------------------------------------------------
# Audit entry points
# ---------------------------------------------------------------------------


async def audit_tools(mcp_server: Any) -> list[AuditFinding]:
    """Audit all registered tools for security compliance.

    Checks performed:

    1. **Missing capability mapping** — every tool exposed by the server
       should have an explicit entry in :data:`~blink.security.capabilities.TOOL_CAPABILITY_MAP`.
    2. **Unknown tools** — tools listed in the capability map but not
       actually registered with the server are flagged as *info*.
    3. **ACT tools without confirmation** — ACT-level tools are only safe
       when ``require_confirmation=True`` in the policy.
    4. **ADMIN capability granted** — the ``admin`` capability should never
       be granted by default; flag it as a critical finding.
    5. **Tool schema validation** — every tool must have a valid
       ``inputSchema`` with ``type: object``.

    Args:
        mcp_server: A live :class:`~blink.mcp.server.MCPServer` instance.

    Returns:
        A list of :class:`AuditFinding` objects.  Empty list means clean.
    """
    findings: list[AuditFinding] = []

    # Fetch registered tools
    tools_response = await mcp_server.handle_tools_list()
    registered_tools: list[dict[str, Any]] = tools_response.get("tools", [])
    registered_names = {t["name"] for t in registered_tools}

    policy = mcp_server._policy  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Check 1: ADMIN capability should never be the granted level
    # ------------------------------------------------------------------
    if policy.granted_capability == Capability.ADMIN:
        findings.append(
            AuditFinding(
                severity="critical",
                tool="*",
                issue=(
                    "The granted capability is set to ADMIN, which gives unrestricted access "
                    "to all tools without confirmation."
                ),
                recommendation=(
                    "Use 'act' capability for normal operation and only escalate to 'admin' "
                    "for specific administrative tasks."
                ),
            )
        )

    # ------------------------------------------------------------------
    # Check 2: ACT tools require confirmation when policy allows ACT
    # ------------------------------------------------------------------
    if (
        policy.granted_capability >= Capability.ACT
        and not policy.require_confirmation
    ):
        act_tools = [
            name
            for name, cap in TOOL_CAPABILITY_MAP.items()
            if cap == Capability.ACT and name in registered_names
        ]
        if act_tools:
            findings.append(
                AuditFinding(
                    severity="warning",
                    tool=", ".join(act_tools),
                    issue=(
                        "ACT-level tools are enabled without requiring user confirmation "
                        "(require_confirmation=False in policy)."
                    ),
                    recommendation=(
                        "Set require_confirmation=True or add auto_approve patterns only "
                        "for commands you trust unconditionally."
                    ),
                )
            )

    # ------------------------------------------------------------------
    # Check 3: Registered tools missing from capability map
    # ------------------------------------------------------------------
    for tool in registered_tools:
        name = tool.get("name", "")
        if name not in TOOL_CAPABILITY_MAP and name not in policy.tool_capabilities:
            findings.append(
                AuditFinding(
                    severity="warning",
                    tool=name,
                    issue=(
                        f"Tool '{name}' is registered but has no explicit capability mapping. "
                        "It will use the default_capability level."
                    ),
                    recommendation=(
                        f"Add '{name}' to TOOL_CAPABILITY_MAP in blink/security/capabilities.py "
                        "with the appropriate capability level."
                    ),
                )
            )

    # ------------------------------------------------------------------
    # Check 4: Tools in capability map but not registered
    # ------------------------------------------------------------------
    for name in TOOL_CAPABILITY_MAP:
        if name not in registered_names:
            findings.append(
                AuditFinding(
                    severity="info",
                    tool=name,
                    issue=(
                        f"Tool '{name}' has a capability mapping but is not registered "
                        "with the current server instance."
                    ),
                    recommendation=(
                        "This is informational — the tool may be intentionally excluded. "
                        "Remove the mapping if the tool is permanently disabled."
                    ),
                )
            )

    # ------------------------------------------------------------------
    # Check 5: Tool schema validation
    # ------------------------------------------------------------------
    for tool in registered_tools:
        name = tool.get("name", "")
        schema = tool.get("inputSchema")
        if not schema:
            findings.append(
                AuditFinding(
                    severity="warning",
                    tool=name,
                    issue=f"Tool '{name}' has no inputSchema.",
                    recommendation="Add an inputSchema to enable parameter validation.",
                )
            )
        elif schema.get("type") != "object":
            findings.append(
                AuditFinding(
                    severity="warning",
                    tool=name,
                    issue=(
                        f"Tool '{name}' inputSchema has type "
                        f"'{schema.get('type')}' instead of 'object'."
                    ),
                    recommendation="MCP tool schemas must have type: object at the top level.",
                )
            )

    # ------------------------------------------------------------------
    # Check 6: Excessively broad auto_approve patterns
    # ------------------------------------------------------------------
    dangerous_auto_approve = ["*", "**", ".*", ".+"]
    for pattern in policy.auto_approve:
        if pattern in dangerous_auto_approve:
            findings.append(
                AuditFinding(
                    severity="critical",
                    tool="*",
                    issue=(
                        f"auto_approve pattern {pattern!r} matches ALL commands, "
                        "bypassing all confirmation prompts."
                    ),
                    recommendation=(
                        "Use specific patterns (e.g. 'git status', 'ls *') instead "
                        "of catch-all wildcards."
                    ),
                )
            )

    return findings


async def audit_report(mcp_server: Any) -> str:
    """Generate a human-readable audit report as a string.

    Args:
        mcp_server: A live :class:`~blink.mcp.server.MCPServer` instance.

    Returns:
        A multi-line report string.
    """
    findings = await audit_tools(mcp_server)

    lines: list[str] = ["Blink MCP Security Audit Report", "=" * 40, ""]

    if not findings:
        lines.append("No findings. All checks passed.")
        return "\n".join(lines)

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    findings_sorted = sorted(findings, key=lambda f: severity_order.get(f.severity, 99))

    counts = {"critical": 0, "warning": 0, "info": 0}
    for f in findings_sorted:
        counts[f.severity] = counts.get(f.severity, 0) + 1
        lines.append(f"[{f.severity.upper()}] {f.tool}")
        lines.append(f"  Issue          : {f.issue}")
        lines.append(f"  Recommendation : {f.recommendation}")
        lines.append("")

    lines.append(
        f"Summary: {counts['critical']} critical, "
        f"{counts['warning']} warnings, "
        f"{counts['info']} info"
    )

    return "\n".join(lines)


__all__ = ["AuditFinding", "audit_tools", "audit_report"]
