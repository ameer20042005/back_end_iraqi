"""Exercise the real boot helpers without installing packages or starting models."""

import subprocess
import time
from pathlib import Path

import pytest

from tests.test_startup_constraints import BASH, ROOT

pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).is_file(), reason="Bash is required")
SOURCE = (ROOT / "start.sh").read_text(encoding="utf-8")


def helper(name):
    start = SOURCE.index(name + "() {")
    end = SOURCE.index("\n}\n", start) + 3
    return SOURCE[start:end]


def bash(tmp_path, code):
    script = tmp_path / "boot-check.sh"
    script.write_text(code, encoding="utf-8")
    return subprocess.run([BASH, script.name], cwd=tmp_path, capture_output=True, text=True, timeout=10)


def test_boot_failure_reports_stage_line_and_status_without_command_contents(tmp_path):
    code = "\n".join([
        "set -Ee", "BOOT_STAGE=initializing", helper("_boot_stage"), helper("_startup_error"),
        "trap '_startup_error \"$?\" \"$LINENO\"' ERR",
        "_boot_stage 'Installing dependencies'", "SENSITIVE_VALUE=private_test_value", "false",
    ])
    result = bash(tmp_path, code)
    assert result.returncode == 1
    assert "Installing dependencies" in result.stdout
    assert "stage=Installing dependencies" in result.stderr
    assert "line=" in result.stderr and "exit=1" in result.stderr
    assert "private_test_value" not in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["_fix_cuda_lib_path", "_fix_cuda_nvcc_path"])
def test_missing_optional_cuda_packages_do_not_abort_startup(tmp_path, name):
    result = bash(tmp_path, "\n".join([
        "set -Ee", "python3() { return 1; }", helper(name), name, "echo reached_next_stage",
    ]))
    assert result.returncode == 0, result.stderr
    assert "reached_next_stage" in result.stdout


def test_bad_model_configuration_aborts_before_launch(tmp_path):
    start = SOURCE.index("STATIC_CONFIG_TEXT=$(")
    end = SOURCE.index('\nMODEL_NAME=', start)
    result = bash(tmp_path, "\n".join([
        "set -e", "python3() { return 19; }", SOURCE[start:end], "echo should_not_launch",
    ]))
    assert result.returncode == 19
    assert "should_not_launch" not in result.stdout


@pytest.mark.parametrize("failed_service,status", [("api", 23), ("vllm", 29)])
def test_supervisor_preserves_failure_and_stops_the_surviving_service(tmp_path, failed_service, status):
    api = f"(sleep 0.1; exit {status})" if failed_service == "api" else "sleep 3"
    vllm = f"(sleep 0.1; exit {status})" if failed_service == "vllm" else "sleep 3"
    code = "\n".join([
        "set -e", helper("_cleanup_services"), helper("_supervise_services"),
        "VLLM_LOG=/tmp/vllm_boot.log",
        "trap '_cleanup_services; wait || true; echo cleaned' EXIT",
        vllm + " &", "VLLM_PID=$!", api + " &", "API_PID=$!", "_supervise_services",
    ])
    started = time.monotonic()
    result = bash(tmp_path, code)
    assert result.returncode == status, result.stderr
    assert time.monotonic() - started < 2
    assert "cleaned" in result.stdout
    assert "stopping the other service" in result.stderr


def test_start_scripts_parse():
    for filename in ("start.sh", "start_local.sh"):
        result = subprocess.run([BASH, "-n", filename], cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
