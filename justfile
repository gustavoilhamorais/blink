# Blink development commands
# Requires: https://github.com/casey/just

# Default: list available commands
default:
    @just --list

# Install project with all dev dependencies
install:
    pip install -e ".[dev]"

# Install project with AI provider dependencies
install-ai:
    pip install -e ".[dev,ai]"

# Run tests
test *args:
    pytest {{args}}

# Run tests with coverage
test-cov:
    pytest --cov=src/blink --cov-report=term-missing --cov-report=html

# Run linter
lint:
    ruff check .

# Auto-fix linting issues
lint-fix:
    ruff check --fix .

# Format code
format:
    ruff format .

# Check formatting without modifying
format-check:
    ruff format --check .

# Run type checker
typecheck:
    mypy src

# Run all CI checks (lint + typecheck + test)
ci: lint format-check typecheck test

# Clean build artifacts
clean:
    rm -rf dist/ build/ *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

# Show blink version
version:
    blink --version
