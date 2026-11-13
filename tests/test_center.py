"""The center layer (SPEC UI-59): the logo menu's helper pages open inside the main page, between the side columns."""

from __future__ import annotations

import pytest

from tests.conftest import World


@pytest.mark.parametrize("path", ["/briefing", "/pages", "/usage", "/spec", "/skills"])
def test_the_helper_pages_open_in_the_center_layer(world: World, path) -> None:
    page = world.client.get(path)
    assert "frame-ancestors 'self'" in page.headers["content-security-policy"]
    assert '<script src="/static/center-frame.js' in page.text
