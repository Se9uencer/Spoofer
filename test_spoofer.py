#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3>=10.7"]
# ///
"""Assert-based self-check for the interpolator and point-list engine. No framework: `uv run test_spoofer.py`."""
from spoofer import _extract_token, haversine_distance_m, interpolate_glide, notify_macos


def test_haversine_known_distances() -> None:
    london = (51.5074, -0.1278)
    paris = (48.8566, 2.3522)
    assert abs(haversine_distance_m(london, paris) - 343_000) / 343_000 < 0.03

    nyc = (40.7128, -74.0060)
    la = (34.0522, -118.2437)
    assert abs(haversine_distance_m(nyc, la) - 3_936_000) / 3_936_000 < 0.03


def test_interpolate_glide_shape() -> None:
    start = (41.8781, -87.6298)  # Chicago
    end = (41.8827, -87.6233)  # ~700m away
    speed, hz = 1.4, 1.0  # walking pace, 1Hz sampling
    points = interpolate_glide(start, end, speed, hz, jitter_m=0.0)

    assert points[0] == start
    assert points[-1] == end
    assert len(points) >= 2

    for i in range(len(points) - 2):  # skip the final, possibly-short segment
        step_dist = haversine_distance_m(points[i], points[i + 1])
        assert abs(step_dist - speed / hz) < 0.5


def test_teleport_is_a_degenerate_glide() -> None:
    # teleport is the same shape (list[Point]) the engine already consumes for glide —
    # a 1-point list, not a special case.
    teleport_points = [(41.8781, -87.6298)]
    glide_points = interpolate_glide((41.8781, -87.6298), (41.8827, -87.6233), 1.4)
    for points in (teleport_points, glide_points):
        assert isinstance(points, list)
        assert all(isinstance(p, tuple) and len(p) == 2 for p in points)



def test_notify_macos_escapes() -> None:
    import subprocess as sp
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return sp.CompletedProcess(args, 0)

    real = sp.run
    sp.run = fake_run  # type: ignore[assignment]
    try:
        notify_macos("Spoofer", 'quote " and slash \\')
    finally:
        sp.run = real  # type: ignore[assignment]
    assert calls, "osascript should have been invoked"
    script = calls[0][2]
    assert "display notification" in script
    assert '\\"' in script or 'quote' in script



def test_lan_token_required() -> None:
    class FakeHeaders(dict):
        def get(self, key, default=None):
            return super().get(key.lower(), default)

    class FakeRequest:
        def __init__(self, headers=None, query=None):
            raw = {k.lower(): v for k, v in (headers or {}).items()}
            self.headers = FakeHeaders(raw)
            self.query_params = query or {}

    assert _extract_token(FakeRequest()) == ""
    assert _extract_token(FakeRequest(headers={"X-Spoofer-Token": "abc"})) == "abc"
    assert _extract_token(FakeRequest(query={"token": "xyz"})) == "xyz"
    assert _extract_token(FakeRequest(headers={"Authorization": "Bearer tok"})) == "tok"
    # header wins over query
    assert (
        _extract_token(FakeRequest(headers={"X-Spoofer-Token": "h"}, query={"token": "q"}))
        == "h"
    )



def demo() -> None:
    test_haversine_known_distances()
    test_interpolate_glide_shape()
    test_teleport_is_a_degenerate_glide()
    test_notify_macos_escapes()
    test_lan_token_required()
    print("all checks passed")


if __name__ == "__main__":
    demo()
