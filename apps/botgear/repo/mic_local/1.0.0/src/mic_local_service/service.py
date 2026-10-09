"""MicLocalService — host-side microphone capture (PortAudio).

The control interface + publish shape are entirely inherited from
``rlx_audio.MicrophoneServiceBase``. This file implements ONLY the three
transport hooks for the local data path:

  _enumerate_devices() — sounddevice.query_devices(), input-capable only
  _open()              — a raw int16 InputStream whose callback streams PCM
  _close()             — stop the stream

``sounddevice`` is imported lazily (via ``_sd()``) so this module imports
fine in environments without PortAudio — unit tests patch ``_sd``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from rlx_audio import MicrophoneServiceBase, resample_s16le

logger = logging.getLogger(__name__)

# PortAudio's paInvalidSampleRate. Raw ALSA hw:X,Y device nodes bypass
# ALSA's plug/dmix layer (unlike "default"/plughw) and reject any rate
# but their own fixed native one outright — see _open()'s fallback.
_PA_INVALID_SAMPLE_RATE = -9997


def _sd():
    """Lazy import of sounddevice — keeps the module importable without
    PortAudio present (tests patch this)."""
    import sounddevice  # noqa: PLC0415
    return sounddevice


class MicLocalService(MicrophoneServiceBase):
    """Cross-platform host microphone. See module docstring."""

    source = "local"

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self._stream: Optional[Any] = None
        # Actual PortAudio capture rate — may differ from
        # self.config.sample_rate (the wire rate) when the device
        # rejected it and _open() fell back to the device's native
        # rate. _on_audio resamples back to the wire rate when they
        # differ, so encode_frame/downstream consumers never see it.
        self._capture_rate: int = 0

    # ─── transport hooks ──────────────────────────────────────────────
    async def _enumerate_devices(self) -> List[Dict[str, Any]]:
        sd = _sd()
        try:
            default_in = sd.default.device[0]
        except Exception:  # noqa: BLE001
            default_in = None
        out: List[Dict[str, Any]] = []
        for idx, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels", 0)) <= 0:
                continue
            out.append({
                "id": str(idx),
                "label": dev.get("name") or f"device {idx}",
                "default": idx == default_in,
            })
        return out

    async def _open(self) -> None:
        if self._stream is not None:
            await self._close()
        sd = _sd()
        device = int(self.config.device_id) if self.config.device_id not in (None, "") else None
        wire_rate = self.config.sample_rate
        try:
            stream = self._start_stream(sd, device, wire_rate)
            self._capture_rate = wire_rate
        except sd.PortAudioError as exc:
            if len(exc.args) < 2 or exc.args[1] != _PA_INVALID_SAMPLE_RATE:
                raise
            # The device won't open at the configured wire rate at all
            # (common for a raw hw:X,Y node whose codec only runs at a
            # fixed rate, e.g. 44100/48000). Retry at its own native
            # rate; _on_audio resamples every captured chunk back down
            # to wire_rate before it's emitted.
            device_info = sd.query_devices(device) if device is not None else sd.query_devices(kind="input")
            native_rate = int(round(device_info["default_samplerate"]))
            logger.warning(
                "mic_local %s: device rejected %sHz (%s) — retrying at its native %sHz, "
                "resampling to %sHz in software",
                self.proxy_id, wire_rate, exc, native_rate, wire_rate,
            )
            stream = self._start_stream(sd, device, native_rate)
            self._capture_rate = native_rate
        stream.start()
        self._stream = stream
        logger.info("mic_local %s: capture started (device=%s rate=%s ch=%s capture_rate=%s)",
                    self.proxy_id, device, wire_rate, self.config.channels, self._capture_rate)

    def _start_stream(self, sd: Any, device: Optional[int], samplerate: int) -> Any:
        # blocksize is computed off the STREAM's own rate (not the wire
        # rate) so each callback still covers frame_ms of audio even
        # when capturing at a device's native rate different from the
        # configured wire rate.
        blocksize = max(1, int(round(samplerate * self.config.frame_ms / 1000)))
        return sd.RawInputStream(
            samplerate=samplerate,
            blocksize=blocksize,
            channels=self.config.channels,
            dtype="int16",
            device=device,
            callback=self._on_audio,
        )

    async def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            logger.exception("mic_local %s: stream close failed", self.proxy_id)

    # ─── PortAudio callback (runs on its own thread) ──────────────────
    def _on_audio(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            logger.debug("mic_local %s: stream status %s", self.proxy_id, status)
        # indata is a CFFI buffer for RawInputStream; bytes() copies the
        # interleaved int16 PCM out.
        pcm = bytes(indata)
        if self._capture_rate and self._capture_rate != self.config.sample_rate:
            # _open() fell back to the device's native rate — bring it
            # back to the configured wire rate before it's encoded.
            pcm = resample_s16le(pcm, self._capture_rate, self.config.sample_rate, self.config.channels)
        # Marshal onto the service loop.
        self.emit_pcm_threadsafe(pcm)
