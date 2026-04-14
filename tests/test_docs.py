"""Tests that verify all documentation files exist and are well-formed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Expected docs
# ---------------------------------------------------------------------------

DOCS_DIR = Path(__file__).parent.parent / "docs"

EXPECTED_DOC_FILES = [
    "architecture.md",
    "protocols.md",
    "security.md",
    "shell-integration.md",
]


# ---------------------------------------------------------------------------
# File existence tests
# ---------------------------------------------------------------------------


class TestDocFilesExist:
    @pytest.mark.parametrize("filename", EXPECTED_DOC_FILES)
    def test_doc_file_exists(self, filename: str) -> None:
        doc_path = DOCS_DIR / filename
        assert doc_path.exists(), f"Missing documentation file: docs/{filename}"

    @pytest.mark.parametrize("filename", EXPECTED_DOC_FILES)
    def test_doc_file_not_empty(self, filename: str) -> None:
        doc_path = DOCS_DIR / filename
        if not doc_path.exists():
            pytest.skip(f"docs/{filename} does not exist")
        content = doc_path.read_text()
        assert len(content.strip()) > 100, f"docs/{filename} appears to be empty or too short"

    @pytest.mark.parametrize("filename", EXPECTED_DOC_FILES)
    def test_doc_file_has_h1_heading(self, filename: str) -> None:
        doc_path = DOCS_DIR / filename
        if not doc_path.exists():
            pytest.skip(f"docs/{filename} does not exist")
        content = doc_path.read_text()
        # Must have at least one H1 or H2 heading
        headings = re.findall(r"^#{1,2}\s+\S+", content, re.MULTILINE)
        assert headings, f"docs/{filename} has no headings (# or ##)"


# ---------------------------------------------------------------------------
# Content checks
# ---------------------------------------------------------------------------


class TestArchitectureDoc:
    """Verify architecture.md covers required sections."""

    @pytest.fixture(autouse=True)
    def load_content(self):
        doc_path = DOCS_DIR / "architecture.md"
        if not doc_path.exists():
            pytest.skip("docs/architecture.md does not exist")
        self.content = doc_path.read_text()

    def test_has_system_overview(self) -> None:
        assert re.search(r"(?i)system overview|overview", self.content)

    def test_has_component_section(self) -> None:
        assert re.search(r"(?i)component", self.content)

    def test_has_data_flow_section(self) -> None:
        assert re.search(r"(?i)data flow|flow", self.content)

    def test_has_ascii_diagram_or_table(self) -> None:
        # Either an ASCII box-drawing diagram or a Markdown table
        has_box = "┌" in self.content or "│" in self.content or "+" in self.content
        has_table = "|" in self.content and "---" in self.content
        assert has_box or has_table, "architecture.md should have a diagram or table"

    def test_mentions_daemon(self) -> None:
        assert "daemon" in self.content.lower()

    def test_mentions_mcp(self) -> None:
        assert "mcp" in self.content.lower()

    def test_mentions_security(self) -> None:
        assert "security" in self.content.lower()

    def test_mentions_sqlite(self) -> None:
        assert "sqlite" in self.content.lower()


class TestProtocolsDoc:
    """Verify protocols.md covers MCP and ACP."""

    @pytest.fixture(autouse=True)
    def load_content(self):
        doc_path = DOCS_DIR / "protocols.md"
        if not doc_path.exists():
            pytest.skip("docs/protocols.md does not exist")
        self.content = doc_path.read_text()

    def test_has_mcp_section(self) -> None:
        assert re.search(r"(?i)\bmcp\b", self.content)

    def test_has_acp_section(self) -> None:
        assert re.search(r"(?i)\bacp\b", self.content)

    def test_has_json_examples(self) -> None:
        # Should have at least one JSON code block
        assert "```json" in self.content or '{"' in self.content

    def test_lists_tool_names(self) -> None:
        # Core tools should be documented
        for tool in ["get_active_session", "run_command", "list_blocks"]:
            assert tool in self.content, f"protocols.md missing tool: {tool}"

    def test_has_resource_table_or_list(self) -> None:
        # Resources should be listed
        assert "terminal://" in self.content

    def test_has_capability_levels(self) -> None:
        for level in ["observe", "suggest", "act", "admin"]:
            assert level in self.content.lower(), f"protocols.md missing capability: {level}"


class TestSecurityDoc:
    """Verify security.md covers the capability model, secrets, and audit log."""

    @pytest.fixture(autouse=True)
    def load_content(self):
        doc_path = DOCS_DIR / "security.md"
        if not doc_path.exists():
            pytest.skip("docs/security.md does not exist")
        self.content = doc_path.read_text()

    def test_has_capability_model_section(self) -> None:
        assert re.search(r"(?i)capability", self.content)

    def test_has_secret_handling_section(self) -> None:
        assert re.search(r"(?i)secret|redact", self.content)

    def test_has_audit_log_section(self) -> None:
        assert re.search(r"(?i)audit", self.content)

    def test_has_sql_schema(self) -> None:
        assert "audit_log" in self.content

    def test_has_best_practices(self) -> None:
        assert re.search(r"(?i)best practice|recommendation|tip", self.content)

    def test_has_rate_limit_section(self) -> None:
        assert re.search(r"(?i)rate.limit", self.content)

    def test_has_input_sanitization_section(self) -> None:
        assert re.search(r"(?i)sanitiz", self.content)

    def test_has_threat_model(self) -> None:
        assert re.search(r"(?i)threat", self.content)


class TestShellIntegrationDoc:
    """Verify shell-integration.md covers installation for bash/zsh/fish."""

    @pytest.fixture(autouse=True)
    def load_content(self):
        doc_path = DOCS_DIR / "shell-integration.md"
        if not doc_path.exists():
            pytest.skip("docs/shell-integration.md does not exist")
        self.content = doc_path.read_text()

    def test_has_bash_instructions(self) -> None:
        assert "bash" in self.content.lower()
        assert ".bashrc" in self.content

    def test_has_zsh_instructions(self) -> None:
        assert "zsh" in self.content.lower()
        assert ".zshrc" in self.content

    def test_has_fish_instructions(self) -> None:
        assert "fish" in self.content.lower()

    def test_has_troubleshooting_section(self) -> None:
        assert re.search(r"(?i)troubleshoot", self.content)

    def test_has_installation_steps(self) -> None:
        assert "source" in self.content

    def test_has_env_var_table(self) -> None:
        assert "BLINK_SESSION_ID" in self.content

    def test_has_verification_steps(self) -> None:
        assert re.search(r"(?i)verif", self.content)

    def test_has_uninstall_instructions(self) -> None:
        assert re.search(r"(?i)uninstall|remov", self.content)


# ---------------------------------------------------------------------------
# Cross-reference checks
# ---------------------------------------------------------------------------


class TestCrossReferences:
    """Verify that internal references between docs are consistent."""

    def test_architecture_references_security(self) -> None:
        arch = DOCS_DIR / "architecture.md"
        if not arch.exists():
            pytest.skip()
        content = arch.read_text()
        # Architecture doc should reference security components
        assert re.search(r"(?i)security", content)

    def test_security_references_capability_checker(self) -> None:
        sec = DOCS_DIR / "security.md"
        if not sec.exists():
            pytest.skip()
        content = sec.read_text()
        assert re.search(r"(?i)capability", content)

    def test_protocols_references_audit_log(self) -> None:
        proto = DOCS_DIR / "protocols.md"
        if not proto.exists():
            pytest.skip()
        content = proto.read_text()
        assert re.search(r"(?i)audit", content)

    def test_shell_integration_references_daemon(self) -> None:
        shell_doc = DOCS_DIR / "shell-integration.md"
        if not shell_doc.exists():
            pytest.skip()
        content = shell_doc.read_text()
        assert re.search(r"(?i)daemon", content)


# ---------------------------------------------------------------------------
# Kitten documentation
# ---------------------------------------------------------------------------


class TestKittenFilesExist:
    """Verify all kitten modules exist and have docstrings."""

    KITTENS_DIR = Path(__file__).parent.parent / "kittens"

    EXPECTED_KITTENS = [
        "agent_overlay",
        "onboarding",
        "session_picker",
        "artifact_viewer",
    ]

    @pytest.mark.parametrize("kitten_name", EXPECTED_KITTENS)
    def test_kitten_dir_exists(self, kitten_name: str) -> None:
        kitten_path = self.KITTENS_DIR / kitten_name
        assert kitten_path.is_dir(), f"Missing kitten directory: kittens/{kitten_name}"

    @pytest.mark.parametrize("kitten_name", EXPECTED_KITTENS)
    def test_kitten_init_exists(self, kitten_name: str) -> None:
        init_path = self.KITTENS_DIR / kitten_name / "__init__.py"
        assert init_path.exists(), f"Missing kitten init: kittens/{kitten_name}/__init__.py"

    @pytest.mark.parametrize("kitten_name", EXPECTED_KITTENS)
    def test_kitten_has_module_docstring(self, kitten_name: str) -> None:
        init_path = self.KITTENS_DIR / kitten_name / "__init__.py"
        if not init_path.exists():
            pytest.skip()
        content = init_path.read_text()
        # Module docstring starts the file (possibly after encoding comment or blank lines)
        assert content.strip().startswith('"""'), (
            f"kittens/{kitten_name}/__init__.py should start with a module docstring"
        )
