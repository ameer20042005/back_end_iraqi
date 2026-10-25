"""Offline checks for repeat boots, partial installs, and persistent model files."""

from importlib import metadata
from types import SimpleNamespace

import pytest

from scripts.check_requirements import missing_requirements
from tests.test_startup_lifecycle import bash, helper
from tests.test_startup_constraints import BASH


@pytest.fixture
def installed(monkeypatch):
    packages = {}

    def distribution(name):
        if name not in packages:
            raise metadata.PackageNotFoundError(name)
        return SimpleNamespace(version=packages[name][0], requires=packages[name][1])

    monkeypatch.setattr(metadata, "distribution", distribution)
    return packages


def test_satisfied_prerelease_stack_and_dependency_cycle(installed):
    installed.update(a=("1.0", ["b>=2"]), b=("2.1rc1", ["a==1.0"]))
    assert missing_requirements(["a==1.0"]) == []


def test_partial_install_and_changed_pin_are_not_skipped(installed):
    installed.update(a=("1.0", ["b>=2"]))
    assert missing_requirements(["a==1.0"]) == ["b: missing"]
    assert missing_requirements(["a==2.0"]) == ["a: installed 1.0, need ==2.0"]


def test_extras_and_platform_markers(installed):
    installed.update(a=("1.0", ['b; extra == "audio"', 'c; python_version < "2"']))
    assert missing_requirements(["a"]) == []
    assert missing_requirements(["a[audio]"]) == ["b: missing"]


def test_chatterbox_is_checked_without_upstream_conflicting_dependencies(installed):
    installed.update(chatterbox=("0.1.7", ["torch==2.6.0"]))
    assert missing_requirements(["chatterbox==0.1.7"], dependencies=False) == []
    assert missing_requirements(["chatterbox==0.1.7"]) == ["torch: missing"]


@pytest.mark.skipif(not BASH, reason="Bash is required")
@pytest.mark.parametrize("needs_install", [False, True])
def test_repeat_boot_skips_installer_and_missing_packages_install_once(tmp_path, needs_install):
    checker = """
checks=0
python3() {
    checks=$((checks + 1))
    if [ "$checks" = 1 ] && [ NEEDS_INSTALL = yes ]; then return 1; fi
    return 0
}
_boot_stage() { :; }
_install_vllm() { echo install >> installs; }
""".replace("NEEDS_INSTALL", "yes" if needs_install else "no")
    result = bash(tmp_path, "set -e\n" + checker + helper("_ensure_python_dependencies") +
                  "\n_ensure_python_dependencies\n_ensure_python_dependencies\n")
    assert result.returncode == 0, result.stderr
    installs = tmp_path / "installs"
    assert (installs.read_text().splitlines() if installs.exists() else []) == (
        ["install"] if needs_install else []
    )
    assert "skipping installation" in result.stdout


@pytest.mark.skipif(not BASH, reason="Bash is required")
def test_failed_install_aborts_before_chatterbox_or_service_launch(tmp_path):
    result = bash(tmp_path, "\n".join([
        "set -e", "python3() { return 1; }", "_boot_stage() { :; }",
        "_install_vllm() { return 19; }", helper("_ensure_python_dependencies"),
        "_ensure_python_dependencies", "echo should_not_launch",
    ]))
    assert result.returncode == 19
    assert "should_not_launch" not in result.stdout


@pytest.mark.skipif(not BASH, reason="Bash is required")
def test_model_cache_survives_repeat_boot_and_respects_custom_paths(tmp_path):
    result = bash(tmp_path, "\n".join([
        "set -e", 'export HF_HOME="$PWD/models"',
        'export HF_HUB_CACHE="$PWD/custom-hub"', 'export HF_XET_CACHE="$PWD/custom-xet"',
        helper("_prepare_model_cache"), "_prepare_model_cache",
        'mkdir -p "$HF_HUB_CACHE/models--example/snapshots/revision"',
        'printf completed > "$HF_HUB_CACHE/models--example/snapshots/revision/model.safetensors"',
        "_prepare_model_cache",
        'test "$(cat "$HF_HUB_CACHE/models--example/snapshots/revision/model.safetensors")" = completed',
        'test "$HF_HOME" = "$PWD/models"', 'test -d "$PWD/custom-xet"',
    ]))
    assert result.returncode == 0, result.stderr
