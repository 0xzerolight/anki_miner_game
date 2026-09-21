"""The worker's streaming speech segmenter against faster-whisper's whole-array algorithm (spec 13.2).

These tests need no add-on environment: the segmenter is plain Python over the model's per-window
probabilities, so they run in the gate. The worker itself is exercised in ``test_vad_worker.py``.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from anki_miner_game.vad.worker import vad_worker as vw
from tests.vad import _fw_reference as ref

WINDOW = 512
SPEECH, SILENCE = 0.9, 0.0


def _stream(probs, audio_length, options=None):
    """All regions the segmenter emits, plus the index of the window that emitted each one."""
    segmenter = vw.SpeechSegmenter(options)
    emitted = []
    for i, prob in enumerate(probs):
        emitted.extend((region, i) for region in segmenter.push(prob))
    emitted.extend((region, None) for region in segmenter.finish(audio_length))
    return emitted


def _regions(probs, audio_length, options=None):
    return [region for region, _ in _stream(probs, audio_length, options)]


def _reference(probs, audio_length, options):
    speeches = ref.get_speech_timestamps(
        probs,
        audio_length,
        threshold=options.threshold,
        neg_threshold=options.neg_threshold,
        min_speech_duration_ms=options.min_speech_duration_ms,
        min_silence_duration_ms=options.min_silence_duration_ms,
        speech_pad_ms=options.speech_pad_ms,
    )
    return [(s["start"], s["end"]) for s in speeches]


def test_default_options_are_the_spec_parameters():
    options = vw.VadOptions()
    assert options.threshold == 0.5
    assert options.neg_threshold is None
    assert options.min_speech_duration_ms == 250
    assert options.min_silence_duration_ms == 300
    assert options.speech_pad_ms == 100


def test_a_region_is_emitted_by_the_window_that_closes_it():
    probs = [SPEECH] * 20 + [SILENCE] * 20
    emitted = _stream(probs, len(probs) * WINDOW)
    # Speech ends at window 20 (sample 10240); 300 ms = 4800 samples of silence close it at window 30.
    assert emitted == [((0, 10240 + 1600), 30)]


def test_speech_shorter_than_the_minimum_is_dropped():
    probs = [SILENCE] * 10 + [SPEECH] * 7 + [SILENCE] * 20  # 7 windows = 3584 samples < 250 ms
    assert _regions(probs, len(probs) * WINDOW) == []


def test_speech_running_to_the_end_closes_at_the_audio_length():
    probs = [SILENCE] * 10 + [SPEECH] * 30
    audio_length = 39 * WINDOW + 100
    assert _stream(probs, audio_length) == [((10 * WINDOW - 1600, audio_length), None)]


def test_padding_is_clamped_to_the_start_of_the_audio():
    probs = [SILENCE] * 2 + [SPEECH] * 20 + [SILENCE] * 20
    assert _regions(probs, len(probs) * WINDOW) == [(0, 22 * WINDOW + 1600)]


def test_no_windows_no_regions():
    assert _regions([], 0) == []


def test_samples_to_ms_floors_the_start_and_ceils_the_end():
    assert vw.region_ms(1600, 11840, 0) == (100, 740)
    assert vw.region_ms(1, 17, 0) == (0, 2)
    assert vw.region_ms(16000, 32000, 250) == (1250, 2250)


# Runs of one probability model real speech/silence stretches; loose floats cover everything else.
_PROB = st.one_of(
    st.sampled_from([0.0, 0.2, 0.34, 0.35, 0.36, 0.49, 0.5, 0.51, 0.9, 1.0]),
    st.floats(0.0, 1.0),
)
_RUNS = st.lists(st.tuples(_PROB, st.integers(1, 40)), min_size=1, max_size=25).map(
    lambda runs: [p for p, n in runs for _ in range(n)]
)
_PROBS = st.one_of(_RUNS, st.lists(_PROB, min_size=1, max_size=300))


_SPEC_OPTIONS = st.just(vw.VadOptions())


@st.composite
def _audio(draw, options=_SPEC_OPTIONS):
    """Per-window probabilities, an audio length consistent with them, and options.

    faster-whisper pads the audio with ``512 - len % 512`` zeros, so n windows always hold
    between 512 * (n - 1) and 512 * n - 1 real samples.
    """
    probs = draw(_PROBS)
    audio_length = WINDOW * (len(probs) - 1) + draw(st.integers(0, WINDOW - 1))
    return probs, audio_length, draw(options)


_OPTIONS = st.builds(
    vw.VadOptions,
    threshold=st.floats(0.05, 0.95),
    min_speech_duration_ms=st.integers(0, 600),
    min_silence_duration_ms=st.integers(0, 2500),
    speech_pad_ms=st.integers(0, 600),
)


@settings(max_examples=400, deadline=None)
@given(_audio())
def test_streaming_matches_faster_whisper_with_the_spec_parameters(case):
    probs, audio_length, options = case
    assert _regions(probs, audio_length, options) == _reference(probs, audio_length, options)


@settings(max_examples=400, deadline=None)
@given(_audio(options=_OPTIONS))
def test_streaming_matches_faster_whisper_for_any_parameters(case):
    """Short silences (below twice the pad) take the reference's split-the-gap branch."""
    probs, audio_length, options = case
    assert _regions(probs, audio_length, options) == _reference(probs, audio_length, options)


@settings(max_examples=200, deadline=None)
@given(_audio())
def test_regions_are_ordered_and_within_the_audio(case):
    probs, audio_length, _ = case
    regions = _regions(probs, audio_length)
    for start, end in regions:
        assert 0 <= start < end <= audio_length
    for (_, prev_end), (next_start, _) in zip(regions, regions[1:], strict=False):
        assert prev_end <= next_start
