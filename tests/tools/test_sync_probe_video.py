"""tools/sync_probe/video.py: luma parsing, flash detection and the ffmpeg decode."""

import pytest

from tests.tools.lavfi import make_flash_video, needs_ffmpeg
from tools.sync_probe import video

LUMA_LOG = """\
frame:0    pts:0       pts_time:0
lavfi.signalstats.YAVG=16
frame:1    pts:33      pts_time:0.033
lavfi.signalstats.YAVG=16.5
frame:2    pts:67      pts_time:0.067
lavfi.signalstats.YAVG=235
"""


def test_parse_luma_log_pairs_pts_time_with_yavg():
    assert video.parse_luma_log(LUMA_LOG) == [(0.0, 16.0), (0.033, 16.5), (0.067, 235.0)]


def test_parse_luma_log_rejects_a_yavg_without_a_frame_line():
    with pytest.raises(video.VideoError):
        video.parse_luma_log("lavfi.signalstats.YAVG=16\n")


def test_detect_flashes_finds_each_rising_edge():
    frames = [(0, 16.0), (33, 16.0), (67, 235.0), (100, 235.0), (133, 16.0), (167, 200.0)]
    result = video.detect_flashes(frames, threshold=128)
    assert result.flashes == [video.Flash(t_ms=67, frame_index=2, luma=235.0), video.Flash(167, 5, 200.0)]
    assert result.starts_bright is False
    assert result.threshold == 128


def test_detect_flashes_threshold_is_inclusive():
    frames = [(0, 16.0), (33, 128.0), (67, 16.0), (100, 127.9)]
    assert video.detect_flashes(frames, threshold=128).flashes == [video.Flash(33, 1, 128.0)]


def test_detect_flashes_does_not_count_a_bright_start():
    frames = [(0, 235.0), (33, 235.0), (67, 16.0), (100, 235.0)]
    result = video.detect_flashes(frames)
    assert result.flashes == [video.Flash(100, 3, 235.0)]
    assert result.starts_bright is True


def test_the_automatic_threshold_is_midway_between_darkest_and_brightest():
    # A flasher window covering a quarter of the canvas only lifts the mean luma a little.
    frames = [(0, 16.0), (33, 16.0), (67, 70.75), (100, 16.0)]
    assert video.auto_threshold(frames) == pytest.approx(43.375)
    assert [f.t_ms for f in video.detect_flashes(frames).flashes] == [67]


def test_the_automatic_threshold_refuses_a_recording_without_contrast():
    with pytest.raises(video.VideoError, match="contrast"):
        video.auto_threshold([(0, 16.0), (33, 17.0), (67, 16.5)])


@needs_ffmpeg
def test_frame_lumas_and_detection_are_exact_to_the_ms(tmp_path):
    path = make_flash_video(tmp_path / "a.mkv", [1.0, 3.5])
    frames = video.frame_lumas(path)
    assert len(frames) == 180
    assert frames[0] == (0, 16.0)
    assert frames[30] == (1000, 235.0)
    result = video.detect_flashes(frames)
    assert [(f.t_ms, f.frame_index) for f in result.flashes] == [(1000, 30), (3500, 105)]


@needs_ffmpeg
def test_frame_lumas_are_relative_to_a_nonzero_container_start(tmp_path):
    path = make_flash_video(tmp_path / "b.mkv", [1.0, 3.5], offset_s=5)
    assert video.container_start(path) == 5.0
    frames = video.frame_lumas(path)
    assert frames[0][0] == 0
    assert [f.t_ms for f in video.detect_flashes(frames).flashes] == [1000, 3500]


@needs_ffmpeg
def test_a_window_smaller_than_the_canvas_is_still_detected(tmp_path):
    path = make_flash_video(tmp_path / "c.mkv", [1.1, 2.2], box="x=40:y=20:w=40:h=24", duration_s=3)
    assert [f.t_ms for f in video.detect_flashes(video.frame_lumas(path)).flashes] == [1100, 2200]


@needs_ffmpeg
def test_frame_lumas_raises_video_error_on_an_undecodable_file(tmp_path):
    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"not a video")
    with pytest.raises(video.VideoError):
        video.frame_lumas(bad)
