"""Speech-to-text for Frame audio, running locally.

faster-whisper on CPU. Free, private, no account, and your voice never
leaves the machine. The model loads once and is reused -- the first load
downloads roughly 150 MB for `base.en`.

Frame gives us SIGNED PCM. 8-bit is the top byte of the int16 (no dither,
about 48 dB); 16-bit is little-endian. Whisper wants float32 at 16 kHz, so
this module owns that conversion and the resampling when the device was
recording at 8 kHz.
"""

from __future__ import annotations

import array
import logging
import math

log = logging.getLogger(__name__)

WHISPER_RATE = 16000
_model = None
_model_name: str | None = None


def load_model(name: str = "base.en", compute_type: str = "int8"):
    """Load (and cache) the Whisper model. First call may download it."""
    global _model, _model_name
    if _model is not None and _model_name == name:
        return _model
    from faster_whisper import WhisperModel

    log.info("loading whisper model %s (%s)", name, compute_type)
    _model = WhisperModel(name, device="cpu", compute_type=compute_type)
    _model_name = name
    return _model


def pcm_to_float32(pcm: bytes, bit_depth: int) -> array.array:
    """Signed Frame PCM -> float32 in [-1, 1]."""
    out = array.array("f")
    if bit_depth == 16:
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if samples.itemsize != 2:                      # pragma: no cover
            raise RuntimeError("unexpected short size")
        import sys
        if sys.byteorder == "big":
            samples.byteswap()
        out.extend(s / 32768.0 for s in samples)
    elif bit_depth == 8:
        signed = array.array("b")
        signed.frombytes(pcm)
        out.extend(s / 128.0 for s in signed)
    else:
        raise ValueError(f"bit_depth must be 8 or 16, got {bit_depth}")
    return out


def resample_to_16k(samples: array.array, src_rate: int) -> array.array:
    """Linear resample. Only ever upsamples 8k -> 16k here, so this is fine."""
    if src_rate == WHISPER_RATE:
        return samples
    ratio = WHISPER_RATE / src_rate
    n_out = int(len(samples) * ratio)
    out = array.array("f", [0.0]) * n_out
    for i in range(n_out):
        pos = i / ratio
        lo = int(pos)
        hi = min(lo + 1, len(samples) - 1)
        frac = pos - lo
        out[i] = samples[lo] * (1 - frac) + samples[hi] * frac
    return out


def rms_dbfs(samples: array.array) -> float:
    """Loudness, for deciding whether anything was actually said."""
    if not samples:
        return -120.0
    mean_sq = sum(s * s for s in samples) / len(samples)
    return 20 * math.log10(math.sqrt(mean_sq)) if mean_sq > 0 else -120.0


def transcribe(pcm: bytes, sample_rate: int, bit_depth: int,
               model_name: str = "base.en",
               min_dbfs: float = -45.0) -> tuple[str, float]:
    """PCM in, (text, loudness_dbfs) out.

    Returns an empty string when the clip is too quiet to be speech, so the
    caller can say "didn't catch that" rather than hallucinating a question
    out of room tone -- Whisper will happily invent text from silence.
    """
    samples = pcm_to_float32(pcm, bit_depth)
    level = rms_dbfs(samples)
    if level < min_dbfs:
        log.info("clip too quiet: %.1f dBFS < %.1f", level, min_dbfs)
        return "", level

    samples = resample_to_16k(samples, sample_rate)

    import numpy as np

    audio = np.frombuffer(samples.tobytes(), dtype=np.float32)
    model = load_model(model_name)
    segments, _info = model.transcribe(
        audio, language="en", beam_size=1, vad_filter=True,
        condition_on_previous_text=False)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text, level
