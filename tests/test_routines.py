"""The report prompts in routines/: each asks for, and posts, its own kind."""
import re

import pytest

from .conftest import REPO_ROOT

ROUTINES = REPO_ROOT / "routines"


@pytest.mark.parametrize("kind", ["morning", "evening", "weekly", "monthly"])
def test_each_report_prompt_asks_for_and_posts_its_own_kind(kind):
    prompt = (ROUTINES / f"{kind}-report.zh.prompt.md").read_text(encoding="utf-8")
    assert re.findall(r"report-status\?kind=(\w+)", prompt) == [kind]
    assert re.findall(r'"kind": "(\w+)"', prompt) == [kind]
    days = [int(n) for n in re.findall(r"/api/bundle\?days=(\d+)", prompt)]
    assert len(days) == 1 and 1 <= days[0] <= 90  # the server caps the window at 90 days
    assert "## 用户背景" in prompt
    assert f"[`{kind}-report.zh.prompt.md`]" in (ROUTINES / "README.md").read_text(encoding="utf-8")
