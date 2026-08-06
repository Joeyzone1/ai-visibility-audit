# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""bof-audit: AI agent visibility auditing.

Import ``bof.suite`` before any suite module. See bof/suite.py.
"""

__version__ = "0.1.0"

#: Bumped whenever scoring weights change. Reports state it, and a change
#: means stored baselines must be rescored before deltas mean anything.
#: 2: the headline became the AI Visibility Score, a 40/35/25 blend of engine
#: readability, social citation surface and agent operability. No signal changed,
#: but a v1 headline and a v2 headline are not the same number.
#: 3: D1, D2 and D3 stopped awarding full marks for absence. A signal with
#: nothing to measure now drops out of its lens's denominator and the rest
#: renormalise, so every page with no controls scores differently than it did.
#: Stored operability figures from v2 are not comparable.
SCORING_VERSION = 3
