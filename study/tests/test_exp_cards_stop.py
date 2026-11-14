"""_stop must tolerate macOS EPERM from killpg on an exited (zombie) worker group."""

import errno
import os
import subprocess
import sys
from unittest import mock

from study.exp.cards import build


def test_stop_tolerates_eperm_from_exited_group(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait()
    process.cards_temp_dir = scratch

    def eperm(pid, sig):
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    with mock.patch.object(build.os, "killpg", side_effect=eperm) as killpg:
        build._stop(process)
    assert killpg.call_count >= 2
    assert not scratch.exists()


def test_stop_tolerates_missing_group(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait()
    process.cards_temp_dir = None
    with mock.patch.object(build.os, "killpg", side_effect=ProcessLookupError):
        build._stop(process)
