"""Exercise the RunPod installer without downloading GPU packages."""

import os
from importlib.metadata import version
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from pip._vendor.packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[1]
BASH = (
    str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
    if os.name == "nt"
    else shutil.which("bash")
)


@pytest.mark.skipif(not BASH or not Path(BASH).is_file(), reason="Bash is required")
@pytest.mark.parametrize("pip_status", [0, 19])
def test_vllm_install_preserves_pins_and_cleans_up_constraints(tmp_path, pip_status):
    source = (ROOT / "start.sh").read_text(encoding="utf-8")
    start = source.index("_install_vllm() (")
    end = source.index("\n)\n", start) + 3
    helper = source[start:end]
    for filename in ("requirements.txt", "requirements-gpu.txt"):
        shutil.copyfile(ROOT / filename, tmp_path / filename)

    # Replace only the expensive pip invocation; execute the real shell helper.
    harness = """set -e
python3() {
    printf '%s\\n' "$@" > pip-args.txt
    while [ "$#" -gt 0 ]; do
        if [ "$1" = '-c' ]; then
            cp -- "$2" constraints-used.txt
            printf '%s' "$2" > constraints-path.txt
            break
        fi
        shift
    done
    return PIP_STATUS
}
""".replace("PIP_STATUS", str(pip_status))
    harness += helper + """
trap 'if [ ! -e "$(cat constraints-path.txt)" ]; then touch cleaned-up; fi' EXIT
_install_vllm
"""
    (tmp_path / "check.sh").write_text(harness, encoding="utf-8")
    result = subprocess.run([BASH, "check.sh"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == pip_status, result.stderr
    assert (tmp_path / "cleaned-up").exists()

    def requirements(text):
        return [Requirement(line.strip()) for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]

    original = requirements("\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("requirements.txt", "requirements-gpu.txt")
    ))
    constraints = requirements((tmp_path / "constraints-used.txt").read_text(encoding="utf-8"))
    assert any(req.extras for req in original)
    assert all(not req.extras for req in constraints)
    assert [(req.name, str(req.specifier), req.marker) for req in constraints] == [
        (req.name, str(req.specifier), req.marker) for req in original
    ]
    assert "vllm[audio]" in (tmp_path / "pip-args.txt").read_text()

    if pip_status == 0:
        # Let the real pip resolver validate the constraints, fully offline.
        check = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--no-index",
             "--no-deps", "-c", str(tmp_path / "constraints-used.txt"),
             "pip==" + version("pip")],
            capture_output=True, text=True,
        )
        assert check.returncode == 0, check.stdout + check.stderr
