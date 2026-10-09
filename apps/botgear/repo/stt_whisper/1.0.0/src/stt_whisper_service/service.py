"""SttWhisperService — Whisper-family (faster-whisper/CTranslate2) speech-to-text.

The listen/start/stop/select_input/set_model/list_models/mute interface, the
source subscription, the recognizer loop, and the utterance queue that
bridges it to listen() are all inherited from rlx_audio.SttServiceBase — the
SAME base stt_local uses (see this type's README.md). This file implements
the four engine hooks, plus buffering/endpointing that a true streaming
recognizer (sherpa-onnx) doesn't need: Whisper transcribes a whole utterance
in one forward pass, not frame-by-frame, so _feed accumulates audio
internally and only calls the model once trailing silence crosses
config.endpoint_silence_ms (or config.max_utterance_s is hit as a safety
cap). Consequence: this engine only ever emits FINALS — no live partials
(last_partial stays empty, /stt/{id}/text only carries final:true) — see
package.yml for the full tradeoff vs. stt_local's low-latency partials.

faster-whisper (CTranslate2) was chosen over the original openai-whisper
(needs torch + a system ffmpeg binary) and whisper.cpp bindings (need a C++
toolchain at pip-install time): no torch, no system ffmpeg (av's wheel
bundles it), prebuilt wheels for Linux x86_64/aarch64 (64-bit Raspberry Pi),
macOS, Windows.

``faster_whisper``/``numpy`` are imported lazily so the module loads without
them (unit tests can patch ``_create_recognizer``/``_ensure_model``), same
convention stt_local uses for sherpa_onnx/numpy.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import Field
from rlx_audio import SttConfig, SttServiceBase, SttState, level_rms
from rlx_bus import service_method

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "base.en"

# Curated sizes surfaced in the picker. faster_whisper.utils._MODELS has the
# full catalog (tiny/tiny.en/base/base.en/small/.../large-v3/distil-large-v3/
# ...) — any of those ids also work via set_model even when not listed here.
_CURATED_MODELS: Dict[str, str] = {
    "tiny.en": "Tiny (English, fastest, least accurate)",
    "base.en": "Base (English, default — fast, meaningfully better than tiny)",
    "small": "Small (multilingual)",
    "medium": "Medium (multilingual, slow on CPU)",
    "distil-large-v3": "Distil-Large-v3 (near-large accuracy, faster)",
    "large-v3": "Large-v3 (best accuracy, slow on CPU)",
}

# RMS gate (0.0-1.0, see rlx_audio.level_rms) below which a frame counts as
# silence for endpointing. Not exposed as config — endpoint_silence_ms
# (inherited from SttConfig, the SAME knob stt_local uses) is the one
# user-facing endpointing control; this is just the loudness floor under it.
_SILENCE_RMS = 0.02


class SttWhisperConfig(SttConfig):
    """Adds only what's genuinely Whisper-specific on top of SttConfig's
    shared input_kind/input_ref/model/sample_rate/channels/muted/continuous/
    endpoint_silence_ms (all reused as-is)."""

    compute_type: str = Field(
        "int8", description="CTranslate2 compute type: int8 (CPU default) | int8_float16 | float16 | float32.")
    device: str = Field(
        "cpu", description="Inference device: cpu | cuda | auto. GPU optional, never required.")
    language: Optional[str] = Field(
        None, description="Force a language code (e.g. 'en'); None = auto-detect per utterance.")
    vad_filter: bool = Field(
        True, description="Run faster-whisper's bundled Silero VAD inside transcribe() to trim non-speech before decoding (complements, doesn't replace, the endpoint_silence_ms buffering gate below).")
    max_utterance_s: float = Field(
        30.0, ge=5.0, le=120.0,
        description="Hard cap on buffered audio before a forced flush, so continuous speech with no long-enough silence gap can't grow the buffer (and transcription delay) unboundedly.")



class WhisperSttState(SttState):
    """Retained on ``/stt/{id}/state`` — the shared STT state plus
    Whisper's batch-transcription flag + language."""
    transcribing: bool
    language: Optional[str] = Field(description="None = auto-detect")

class SttWhisperService(SttServiceBase):
    """Whisper-family offline ASR (faster-whisper/CTranslate2). See module docstring."""

    source = "whisper"
    config_class = SttWhisperConfig
    state_schema = WhisperSttState
    DEFAULT_MODEL = DEFAULT_MODEL

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._transcribing = False   # UI-facing: a blocking transcribe() call is in flight

    # ─── engine hooks ─────────────────────────────────────────────────
    def _list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": mid, "label": label, "downloaded": self._is_downloaded(mid)}
            for mid, label in _CURATED_MODELS.items()
        ]

    def _is_downloaded(self, model_id: str) -> bool:
        from faster_whisper.utils import download_model  # noqa: PLC0415 — lazy, see module docstring
        try:
            download_model(model_id, cache_dir=str(self.models_dir), local_files_only=True)
            return True
        except Exception:  # noqa: BLE001 — huggingface_hub's "not cached" exception, broad on purpose
            return False

    def _ensure_model(self, model_id: str) -> Path:
        """Download (or resolve the already-cached) model snapshot dir. Runs
        in a worker thread — this is the ONLY network call; _create_recognizer
        then builds from the local path with no further downloading.

        NOTE: models_dir resolves to ``<data>/stt/models/`` — the SAME
        directory stt_local's sherpa-onnx tarballs extract into, because
        both engines share ``type_name = "stt"`` (see this type's README.md).
        This is safe: sherpa-onnx's dirs (bare model-name directories
        with a tokens.txt) and faster-whisper's Hugging Face Hub cache dirs
        (``models--Systran--faster-whisper-<id>/...`` + a ``.locks/`` dir)
        use non-colliding naming, and each engine's disk-scan only recognizes
        its own shape.
        """
        from faster_whisper.utils import download_model  # noqa: PLC0415
        self.models_dir.mkdir(parents=True, exist_ok=True)
        model_dir = download_model(model_id, cache_dir=str(self.models_dir))
        return Path(model_dir)

    def _create_recognizer(self, model_dir, sample_rate: int) -> Dict[str, Any]:
        from faster_whisper import WhisperModel  # noqa: PLC0415
        model = WhisperModel(str(model_dir), device=self.config.device, compute_type=self.config.compute_type)
        return self._fresh_handle(model, sample_rate)

    @staticmethod
    def _fresh_handle(model: Any, sample_rate: int) -> Dict[str, Any]:
        return {"model": model, "sample_rate": sample_rate, "buffer": bytearray(),
                "speech_seen": False, "silence_ms": 0.0}

    def _feed(self, handle: Dict[str, Any], pcm: bytes) -> Optional[Dict[str, Any]]:
        """Accumulate ``pcm`` into the utterance buffer and RMS-gate trailing
        silence (see module docstring — Whisper isn't frame-incremental like
        sherpa-onnx, so endpointing has to happen here rather than inside the
        engine). Only calls the model — and only then returns a result — once
        an endpoint (or the max_utterance_s safety cap) is reached; otherwise
        returns None (no partials)."""
        handle["buffer"] += pcm
        sample_rate = handle["sample_rate"]
        frame_ms = (len(pcm) / 2) / max(1, sample_rate) * 1000.0
        if level_rms(pcm) >= _SILENCE_RMS:
            handle["speech_seen"] = True
            handle["silence_ms"] = 0.0
        else:
            handle["silence_ms"] += frame_ms

        buffered_ms = (len(handle["buffer"]) / 2) / max(1, sample_rate) * 1000.0
        endpoint = handle["speech_seen"] and handle["silence_ms"] >= self.config.endpoint_silence_ms
        overrun = buffered_ms >= self.config.max_utterance_s * 1000.0
        if not (endpoint or overrun):
            return None

        buf = bytes(handle["buffer"])
        speech_seen = handle["speech_seen"]
        self._reset_buffer(handle)
        if not speech_seen or len(buf) < 2:
            return {"text": "", "final": True}   # nothing but silence buffered (max_utterance_s safety net)

        text = self._transcribe(handle["model"], buf, sample_rate)
        return {"text": text, "final": True}

    def _transcribe(self, model: Any, pcm: bytes, sample_rate: int) -> str:
        import numpy as np  # noqa: PLC0415
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        self._set_transcribing(True)
        try:
            segments, _info = model.transcribe(
                audio, language=self.config.language, vad_filter=self.config.vad_filter)
            return " ".join(s.text.strip() for s in segments).strip()
        finally:
            self._set_transcribing(False)

    def _set_transcribing(self, flag: bool) -> None:
        """Publish state around a blocking transcribe() call so the UI can
        show a "transcribing…" indicator during the multi-hundred-ms-to-
        several-second gap between endpoint and final text that stt_local's
        UI never has to account for. Runs on the recognizer thread — hops
        back onto the service's event loop via run_coroutine_threadsafe
        rather than awaiting publish() directly."""
        self._transcribing = flag
        loop = self._loop
        if loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._publish_state(), loop)
            except RuntimeError:
                pass

    @staticmethod
    def _reset_buffer(handle: Dict[str, Any]) -> None:
        handle["buffer"] = bytearray()
        handle["speech_seen"] = False
        handle["silence_ms"] = 0.0

    def _reset_recognizer(self, handle: Dict[str, Any]) -> None:
        """Drop any buffered-but-unfinalized audio so a new utterance after a
        stop/source-switch/mute doesn't carry residue from an aborted one."""
        self._reset_buffer(handle)

    def _free_recognizer(self, handle: Dict[str, Any]) -> None:
        """Release the native CTranslate2 model ref so it's freed promptly on
        shutdown rather than at interpreter teardown."""
        handle["model"] = None

    # ─── whisper-specific extras beyond the transcription capability ───
    # (language/compute_type/device/vad_filter/max_utterance_s are all
    # wizard_config fields, settable at creation time; set_language is the
    # one worth a LIVE action too, for parity with the base capability's
    # own set_model — the rest are advanced/rare enough to not need it.)
    @service_method("set_language", publishes=["state"])
    async def m_set_language(self, language: Optional[str] = None) -> Dict[str, Any]:
        """Force a language code (e.g. 'en') for subsequent utterances, or
        clear it (None) to auto-detect per utterance."""
        await self.update_config({"language": (language or "").strip() or None})
        await self._publish_state()
        return self._snapshot()

    # ─── UI-facing extra state ─────────────────────────────────────────
    def _snapshot(self) -> Dict[str, Any]:
        state = WhisperSttState(
            **super()._snapshot(),
            transcribing=self._transcribing,
            language=self.config.language,
        )
        return state.model_dump()
