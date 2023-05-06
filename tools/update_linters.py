"""Update the linters used in CI and for pre-commit hooks."""

import re
import subprocess
import sys
from pathlib import Path

pre_commit_header = """
# DO NOT EDIT THIS FILE DIRECTLY
# This file is autogenerated by tools/update_linters.py

# To use pre-commit, run `pip install pre-commit && pre-commit install`

repos:
"""

pre_commit_sections = {
    "pre-commit-hooks": """  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v{version}
    hooks:
      - id: check-merge-conflict
      - id: check-yaml
      - id: end-of-file-fixer
      - id: trailing-whitespace""",
    "ruff": """  - repo: https://github.com/charliermarsh/ruff-pre-commit
    rev: "v{version}"
    hooks:
      - id: ruff
        args: [ --fix]""",
    "black": """  - repo: https://github.com/psf/black
    rev: {version}
    hooks:
      - id: black-jupyter""",
    "blacken-docs": """  - repo: https://github.com/asottile/blacken-docs
    rev: {version}
    hooks:
      - id: blacken-docs""",
}


def install_most_recent(package_name) -> str:
    """Install the most recent version and return the version string."""
    print(f"Installing {package_name}...")
    pip_install_result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", package_name],
        check=True,
        capture_output=True,
    )
    result_lines = pip_install_result.stdout.decode("utf-8").splitlines()
    if result_lines[0].startswith(f"Requirement already satisfied: {package_name}"):
        return result_lines[0].split()[-1][1:-1]
    package_version = result_lines[-1].split(" ")[-1]
    reported_package, reported_version = package_version.split("-")
    assert reported_package == package_name
    return reported_version


def update_tox(version_map):
    """Update pinned versions of all packages in tox.ini."""
    tox_text = Path("tox.ini").read_text()
    for package_name, version in version_map.items():
        if f"{package_name}==" not in tox_text:
            continue
        print(f"Updating {package_name} to {version} in tox.ini...")
        package_pattern = re.compile(rf"{package_name}==\S+")
        new_version = f"{package_name}=={version}"
        tox_text = package_pattern.sub(new_version, tox_text)
    Path("tox.ini").write_text(tox_text)


if __name__ == "__main__":
    version_map = {}
    pre_commit_hooks = []
    for package_name, pre_commit_section in pre_commit_sections.items():
        current_version = install_most_recent(package_name)
        version_map[package_name] = current_version
        pre_commit_hooks.append(pre_commit_section.format(version=current_version))

    pre_commit_text = pre_commit_header + "\n".join(pre_commit_hooks) + "\n"
    Path(".pre-commit-config.yaml").write_text(pre_commit_text)
    update_tox(version_map)