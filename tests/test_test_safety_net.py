"""The suite-wide safety net refuses destructive commands against real paths.

Every payload here is harmless even if the net failed: they all target paths
that do not exist (and rm -f / ignore_errors never error on them).
"""

import os

import _pytest.outcomes
import pytest

import swarm.tools.shell as shell

HOME = os.path.realpath(os.path.expanduser("~"))
MISSING = ".swarm-safety-net-canary-does-not-exist"


@pytest.mark.parametrize("cmd", [
    f"rm -f ~/{MISSING}",
    f"rm -f {HOME}/{MISSING}",
    f"rm -rf $HOME/{MISSING}",
])
def test_net_refuses_destructive_shell_commands(cmd):
    with pytest.raises(_pytest.outcomes.Failed, match="TEST SAFETY NET"):
        shell.run(cmd)


def test_net_refuses_destructive_python_scripts(tmp_path):
    script = tmp_path / "x.py"
    script.write_text(f"import shutil, os\nshutil.rmtree(os.path.expanduser('~/{MISSING}'), ignore_errors=True)\n")
    with pytest.raises(_pytest.outcomes.Failed, match="TEST SAFETY NET"):
        shell.run(f"python3 {script}")


def test_net_allows_ordinary_commands(tmp_path):
    (tmp_path / "f").write_text("x")
    assert shell.run(f"rm -f {tmp_path}/f")[0] == 0
    assert shell.run(f"cat {HOME}/{MISSING} 2>/dev/null; echo ok")[1].strip() == "ok"
