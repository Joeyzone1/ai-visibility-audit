# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Start the bof-audit UI from anywhere, including from the OS shell.

    python serve_ui.py [--port 8610]

This exists because a parent launcher may start this with a different working
directory, which breaks two things at once: `python -m bof.serve`
cannot find the package, and serve.py resolves its audits directory from the
working directory, so reports would land in the workflow repo instead of here.

Both are fixed by pinning the working directory to this file's own folder
before importing anything. Running this by absolute path works from any cwd.
"""

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from bof.serve import main  # noqa: E402  # must follow the sys.path pin

if __name__ == "__main__":
    raise SystemExit(main())
