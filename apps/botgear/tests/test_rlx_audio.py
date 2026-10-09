# unmanaged
"""Shared audio-device capability (rlx_audio): frames + control + recording.

Locks the canonical wire shape + the control-method behaviour every
microphone/speaker service type inherits (connect/disconnect/mute/unmute/
select + last_connected_source), the save-to-file path, and speaker
playback feed.
"""
from __future__ import annotations

import array
import asyncio
import struct
import threading
import time
import wave
from typing import Any, Dict, List

import pytest

from rlx_audio import (
    MicrophoneServiceBase,
    SpeakerServiceBase,
    SpeechServiceBase,
    SttServiceBase,
    decode_frame,
    encode_frame,
    level_rms,
    read_wav_pcm,
    remix_channels,
    resample_s16le,
)


# ─── frame schema ─────────────────────────────────────────────────────


def test_frame_round_trip():
    pcm = struct.pack("<4h", 0, 1000, -1000, 32767)
    frame = encode_frame(seq=7, ts=123.5, sample_rate=16000, channels=1, pcm=pcm)
    assert frame["format"] == "pcm_s16le"
    assert decode_frame(frame) == pcm


def test_level_rms():
    assert level_rms(b"") == 0.0
    assert level_rms(struct.pack("<8h", *([0] * 8))) == 0.0
    assert level_rms(struct.pack("<8h", *([32767] * 8))) > 0.99
    assert 0.0 <= level_rms(b"\x01\x02\x03") <= 1.0   # odd length tolerated


# ─── fakes ────────────────────────────────────────────────────────────


class FakeBus:
    def __init__(self) -> None:
        self.published: List[tuple] = []

    async def publish(self, topic: str, payload: Any, *, retained: bool = False, **_: Any) -> None:
        self.published.append((topic, payload, retained))

    async def subscribe(self, topic: str, handler: Any, **_: Any) -> None:
        pass

    def last(self, suffix: str) -> Dict[str, Any]:
        for topic, payload, _ in reversed(self.published):
            if topic.endswith(suffix):
                return payload
        raise AssertionError(f"no publish ending in {suffix!r}")


class FakeMic(MicrophoneServiceBase):
    source = "local"

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self.opened = False
        self.closed = False

    async def _enumerate_devices(self):
        return [{"id": "0", "label": "Mic A", "default": True}]

    async def _open(self):
        self.opened = True

    async def _close(self):
        self.closed = True


class FakeSpeaker(SpeakerServiceBase):
    source = "local"

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self.played: List[bytes] = []

    async def _enumerate_devices(self):
        return [{"id": "0", "label": "Speaker A", "default": True}]

    async def _open(self):
        pass

    async def _close(self):
        pass

    async def _play_pcm(self, pcm, frame):
        self.played.append(pcm)


def _mic():
    return FakeMic("microphone-1", FakeBus())


def _spk():
    return FakeSpeaker("speaker-1", FakeBus())


# ─── shared control interface ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_records_last_connected_source():
    mic = _mic()
    await mic.m_select_device("0")
    await mic.m_connect()
    assert mic.opened is True
    assert mic._connected is True
    assert mic.config.connected is True
    assert mic.config.last_connected_source == "0"
    state = mic.bus.last("/microphone/microphone-1/state")
    assert state["connected"] is True
    assert state["last_connected_source"] == "0"
    assert state["kind"] == "microphone"
    assert state["source"] == "local"


@pytest.mark.asyncio
async def test_disconnect_keeps_last_connected_source():
    mic = _mic()
    await mic.m_select_device("0")
    await mic.m_connect()
    await mic.m_disconnect()
    assert mic.closed is True
    assert mic._connected is False
    assert mic.config.last_connected_source == "0"   # remembered


@pytest.mark.asyncio
async def test_mute_unmute_toggles_and_gates_audio():
    mic = _mic()
    await mic.m_connect()
    await mic.m_mute()
    assert mic.muted is True
    # While muted, captured PCM produces no audio frame.
    before = len(mic.bus.published)
    await mic._emit_pcm(struct.pack("<2h", 100, 200))
    assert all(not t.endswith("/audio") for t, _, _ in mic.bus.published[before:])
    await mic.m_unmute()
    assert mic.muted is False
    await mic._emit_pcm(struct.pack("<2h", 100, 200))
    assert mic.bus.last("/microphone/microphone-1/audio")["format"] == "pcm_s16le"


@pytest.mark.asyncio
async def test_connect_open_failure_surfaces():
    mic = _mic()
    async def boom():
        raise RuntimeError("device busy")
    mic._open = boom  # type: ignore[assignment]
    await mic.m_connect()
    assert mic._connected is False
    assert "device busy" in (mic.bus.last("/microphone/microphone-1/state")["last_error"] or "")


# ─── save-to-file ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recording_writes_wav(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTGEAR_DATA_DIR_ABS", str(tmp_path))
    mic = _mic()
    await mic.m_connect()
    out = await mic.m_start_recording("clip.wav")
    state = mic.bus.last("/microphone/microphone-1/state")
    assert state["recording"] is True
    assert state["recording_path"].endswith("clip.wav")
    # Feed two frames, then stop.
    pcm = struct.pack("<160h", *([1000] * 160))
    await mic._emit_pcm(pcm)
    await mic._emit_pcm(pcm)
    res = await mic.m_stop_recording()
    assert res["bytes"] == len(pcm) * 2
    path = (tmp_path / "recordings" / "clip.wav")
    assert path.is_file()
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == mic.config.sample_rate
        assert w.getnchannels() == mic.config.channels
        assert w.getnframes() == 320            # 160 samples × 2 frames
    # A fresh suggestion is pre-loaded for next time.
    assert mic.bus.last("/microphone/microphone-1/state")["recording_suggested_path"].endswith(".wav")


@pytest.mark.asyncio
async def test_suggested_path_under_recordings_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTGEAR_DATA_DIR_ABS", str(tmp_path))
    mic = _mic()
    p = mic._fresh_suggested_path()
    assert p.startswith(str(tmp_path / "recordings"))
    assert p.endswith(".wav")
    assert "microphone-1" in p


# ─── speaker playback feed ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_speaker_plays_default_sink_when_connected():
    spk = _spk()
    await spk.m_connect()
    pcm = struct.pack("<4h", 1, 2, 3, 4)
    await spk._on_sink_frame(encode_frame(1, 0.0, 16000, 1, pcm))
    assert spk.played == [pcm]
    assert spk.bus.last("/speaker/speaker-1/state")["kind"] == "speaker"


@pytest.mark.asyncio
async def test_speaker_silent_when_muted_or_disconnected():
    spk = _spk()
    frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 1, 2))
    await spk._on_sink_frame(frame)            # disconnected → ignored
    assert spk.played == []
    await spk.m_connect()
    await spk.m_mute()
    await spk._on_sink_frame(frame)            # muted → ignored
    assert spk.played == []


@pytest.mark.asyncio
async def test_speaker_select_input_topic_routes_source_not_sink():
    spk = _spk()
    await spk.m_connect()
    out = await spk.m_select_input("topic", "/microphone/mic-1/audio")
    assert out["input_kind"] == "topic"
    assert out["input_ref"] == "/microphone/mic-1/audio"
    frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 7, 8))
    # In topic mode the default sink is ignored…
    await spk._on_sink_frame(frame)
    assert spk.played == []
    # …and only the matching source topic plays.
    await spk._on_topic_frame(frame, "/microphone/mic-1/audio")
    assert len(spk.played) == 1
    # A stale subscription for some other topic no-ops.
    await spk._on_topic_frame(frame, "/microphone/other/audio")
    assert len(spk.played) == 1


@pytest.mark.asyncio
async def test_speaker_select_input_rejects_unknown_kind():
    from fastapi import HTTPException
    spk = _spk()
    with pytest.raises(HTTPException):
        await spk.m_select_input("carrier-pigeon", "x")


# ─── speaker volume + transport (media-player controls) ───────────────


@pytest.mark.asyncio
async def test_speaker_volume_clamps_and_scales():
    spk = _spk()
    await spk.m_set_volume(0.5)
    assert spk.config.volume == 0.5
    assert spk.bus.last("/speaker/speaker-1/state")["volume"] == 0.5
    await spk.m_set_volume(5)      # clamp high
    assert spk.config.volume == 1.0
    await spk.m_set_volume(-1)     # clamp low
    assert spk.config.volume == 0.0
    # _scale_volume: full → unchanged, zero → silence.
    await spk.m_set_volume(1.0)
    assert spk._scale_volume(struct.pack("<2h", 1000, -1000)) == struct.pack("<2h", 1000, -1000)
    await spk.m_set_volume(0.0)
    assert spk._scale_volume(struct.pack("<2h", 1000, -1000)) == struct.pack("<2h", 0, 0)


@pytest.mark.asyncio
async def test_speaker_transport_state():
    spk = _spk()
    spk._duration_s = 30.0
    await spk.m_play()
    s = spk.bus.last("/speaker/speaker-1/state")
    assert s["playing"] is True and s["paused"] is False
    await spk.m_pause()
    assert spk.bus.last("/speaker/speaker-1/state")["paused"] is True
    await spk.m_seek(10)
    assert spk.bus.last("/speaker/speaker-1/state")["position_s"] == 10.0
    await spk.m_skip(5)            # FF +5
    assert spk._position_s == 15.0
    await spk.m_skip(-100)         # rewind clamps to 0
    assert spk._position_s == 0.0
    await spk.m_seek(999)          # clamp to duration
    assert spk._position_s == 30.0
    await spk.m_stop()
    s = spk.bus.last("/speaker/speaker-1/state")
    assert s["playing"] is False and s["position_s"] == 0.0


# ─── play set (playlist) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_playlist_add_remove_move_clear():
    spk = _spk()
    await spk.m_playlist_add([{"kind": "file", "ref": "/a.wav"}, {"ref": "https://x/b.mp3"}])
    pl = spk.bus.last("/speaker/speaker-1/state")["playlist"]
    assert [i["ref"] for i in pl] == ["/a.wav", "https://x/b.mp3"]
    assert pl[1]["kind"] == "url"                 # inferred from http
    assert pl[0]["name"] == "a.wav"               # inferred from ref
    await spk.m_playlist_add([{"ref": "/c.flac"}])
    await spk.m_playlist_move(2, 0)               # c to front
    assert [i["name"] for i in spk.bus.last("/speaker/speaker-1/state")["playlist"]] == ["c.flac", "a.wav", "b.mp3"]
    await spk.m_playlist_remove(1)                # remove a.wav
    assert [i["name"] for i in spk.bus.last("/speaker/speaker-1/state")["playlist"]] == ["c.flac", "b.mp3"]
    await spk.m_playlist_clear()
    assert spk.bus.last("/speaker/speaker-1/state")["playlist"] == []


@pytest.mark.asyncio
async def test_play_index_sets_input():
    spk = _spk()
    await spk.m_playlist_set([{"ref": "/a.wav"}, {"ref": "/b.wav"}])
    await spk.m_connect()
    await spk.m_play_index(1)
    assert spk.config.playlist_index == 1
    assert spk.config.input_kind == "file"
    assert spk.config.input_ref == "/b.wav"
    assert spk._playing is True


@pytest.mark.asyncio
async def test_advance_repeat_off_stops_at_end():
    spk = _spk()
    await spk.m_playlist_set([{"ref": "/a.wav"}, {"ref": "/b.wav"}])
    await spk.m_connect()
    await spk.m_play_index(1)                      # last track
    await spk._on_track_ended()                    # auto-advance past end
    assert spk._playing is False                   # repeat off → stop


@pytest.mark.asyncio
async def test_advance_repeat_all_wraps():
    spk = _spk()
    await spk.m_playlist_set([{"ref": "/a.wav"}, {"ref": "/b.wav"}])
    await spk.m_connect()
    await spk.m_set_repeat("all")
    await spk.m_play_index(1)
    await spk._on_track_ended()
    assert spk.config.playlist_index == 0          # wrapped to start
    assert spk._playing is True


@pytest.mark.asyncio
async def test_repeat_one_replays_same_track():
    spk = _spk()
    await spk.m_playlist_set([{"ref": "/a.wav"}, {"ref": "/b.wav"}])
    await spk.m_connect()
    await spk.m_set_repeat("one")
    await spk.m_play_index(0)
    await spk._on_track_ended()
    assert spk.config.playlist_index == 0          # same track
    assert spk.config.input_ref == "/a.wav"


@pytest.mark.asyncio
async def test_next_previous():
    spk = _spk()
    await spk.m_playlist_set([{"ref": "/a.wav"}, {"ref": "/b.wav"}, {"ref": "/c.wav"}])
    await spk.m_connect()
    await spk.m_play_index(0)
    await spk.m_next_track()
    assert spk.config.playlist_index == 1
    await spk.m_previous_track()
    assert spk.config.playlist_index == 0


@pytest.mark.asyncio
async def test_shuffle_covers_all_tracks():
    spk = _spk()
    await spk.m_playlist_set([{"ref": f"/{c}.wav"} for c in "abcde"])
    await spk.m_connect()
    await spk.m_set_shuffle(True)
    await spk.m_set_repeat("all")                  # wrap so next walks the whole order
    await spk.m_play_index(0)
    seen = {spk.config.playlist_index}
    for _ in range(4):
        await spk.m_next_track()
        seen.add(spk.config.playlist_index)
    assert seen == {0, 1, 2, 3, 4}                 # shuffle visits every track once


@pytest.mark.asyncio
async def test_speaker_auto_connects_on_start():
    # Speakers are always-on: starting the service opens the device with no
    # explicit connect (the connect/disconnect UI was removed).
    spk = _spk()
    await spk.on_start()
    assert spk._connected is True
    assert spk.bus.last("/speaker/speaker-1/state")["connected"] is True


@pytest.mark.asyncio
async def test_microphone_does_not_auto_connect():
    # Mics still require an explicit connect (capturing is a deliberate act).
    mic = _mic()
    await mic.on_start()
    assert mic._connected is False


# ─── PCM conditioning helpers ─────────────────────────────────────────


def _tone(samples: int, value: int = 8000, channels: int = 1) -> bytes:
    a = array.array("h", [value] * (samples * channels))
    return a.tobytes()


def test_resample_s16le_changes_length_and_is_identity_at_same_rate():
    pcm = _tone(100)
    assert resample_s16le(pcm, 16000, 16000) == pcm                 # no-op
    down = resample_s16le(pcm, 22050, 16000)
    # 100 input samples @22050 → ~72 @16000.
    assert 60 <= len(down) // 2 <= 80
    up = resample_s16le(pcm, 8000, 16000)
    assert len(up) // 2 > 100


def test_remix_channels_mono_stereo_round_trip():
    mono = array.array("h", [100, 200, 300]).tobytes()
    stereo = remix_channels(mono, 1, 2)
    assert array.array("h", stereo).tolist() == [100, 100, 200, 200, 300, 300]
    back = remix_channels(stereo, 2, 1)
    assert array.array("h", back).tolist() == [100, 200, 300]
    assert remix_channels(mono, 1, 1) == mono                       # no-op


def test_read_wav_pcm(tmp_path):
    path = tmp_path / "x.wav"
    pcm = _tone(160)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(pcm)
    got, rate, ch = read_wav_pcm(str(path))
    assert got == pcm and rate == 22050 and ch == 1


# ─── speech (text-to-speech) ──────────────────────────────────────────


class FakeSpeech(SpeechServiceBase):
    """A speech service whose 'engine' writes a fixed-length tone WAV at a
    native rate that DIFFERS from the wire rate, so the base's conditioning
    (resample → wire rate) is exercised."""

    source = "local"
    NATIVE_RATE = 22050

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self.synth_calls: List[str] = []

    def _synthesize(self, text: str, out_path: str, opts) -> None:
        self.synth_calls.append(text)
        # ~40ms of tone at the native rate (keeps streaming tests fast).
        pcm = _tone(int(self.NATIVE_RATE * 0.04))
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.NATIVE_RATE)
            w.writeframes(pcm)

    def _list_voices(self):
        return [{"id": "test-voice", "label": "Test", "downloaded": True}]


def _speech(tmp_path, monkeypatch) -> FakeSpeech:
    monkeypatch.setenv("BOTGEAR_DATA_DIR_ABS", str(tmp_path))
    return FakeSpeech("speech-1", FakeBus())


@pytest.mark.asyncio
async def test_speech_cache_key_stable_and_sensitive(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    k1 = spk._cache_key("amy", 1.0, "hello")
    assert k1 == spk._cache_key("amy", 1.0, "hello")          # stable
    assert k1 != spk._cache_key("amy", 1.0, "world")          # text
    assert k1 != spk._cache_key("ryan", 1.0, "hello")         # voice
    assert k1 != spk._cache_key("amy", 1.5, "hello")          # rate


@pytest.mark.asyncio
async def test_speech_build_cache_entry_conditions_to_wire_rate(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    spk.cache_dir.mkdir(parents=True, exist_ok=True)
    out = spk.cache_dir / "x.wav"
    spk._build_cache_entry("hi", str(out), "test-voice", 1.0)
    assert out.is_file()
    _, rate, ch = read_wav_pcm(str(out))
    assert rate == spk.config.sample_rate == 16000            # resampled to wire rate
    assert ch == 1
    assert not (spk.cache_dir / "x.wav.tmp.wav").exists()     # temp cleaned up


@pytest.mark.asyncio
async def test_speech_say_enqueues_and_reports_cache(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    res = await spk.m_speak("hello there")
    assert res["queued"] == 1
    assert res["cached"] is False                             # not synthesized yet
    assert spk.bus.last("/speech/speech-1/state")["queue"] == ["hello there"]


@pytest.mark.asyncio
async def test_speech_speak_item_streams_frames_and_caches(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    spk.cache_dir.mkdir(parents=True, exist_ok=True)
    item = {"text": "hi", "voice": "test-voice", "rate": 1.0, "gen": spk._gen}
    await spk._speak_item(item)
    audio = [p for t, p, _ in spk.bus.published if t.endswith("/audio")]
    assert audio and all(f["format"] == "pcm_s16le" for f in audio)
    assert audio[0]["sample_rate"] == 16000                   # frames carry the wire rate
    spoken = spk.bus.last("/speech/speech-1/spoken")
    assert spoken["text"] == "hi" and spoken["cached"] is False
    # Re-saying the same text is a cache hit (no second synth call).
    assert spk.synth_calls == ["hi"]
    await spk._speak_item({"text": "hi", "voice": "test-voice", "rate": 1.0, "gen": spk._gen})
    assert spk.synth_calls == ["hi"]                          # served from cache


@pytest.mark.asyncio
async def test_speech_mute_gates_audio_but_not_state(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    spk.cache_dir.mkdir(parents=True, exist_ok=True)
    await spk.m_mute()
    before = len(spk.bus.published)
    await spk._speak_item({"text": "quiet", "voice": "test-voice", "rate": 1.0, "gen": spk._gen})
    new = spk.bus.published[before:]
    assert not any(t.endswith("/audio") for t, _, _ in new)   # muted → no frames
    assert any(t.endswith("/state") for t, _, _ in new)       # but state still flows


@pytest.mark.asyncio
async def test_speech_stop_interrupts_and_clears_queue(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    await spk.m_speak("one")
    await spk.m_speak("two")
    assert spk._pending == ["one", "two"]
    gen0 = spk._gen
    await spk.m_stop()
    assert spk._gen == gen0 + 1                               # generation bumped
    assert spk._pending == []                                 # queue cleared
    assert spk.bus.last("/speech/speech-1/state")["queue"] == []


@pytest.mark.asyncio
async def test_speech_clear_cache(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    spk.cache_dir.mkdir(parents=True, exist_ok=True)
    (spk.cache_dir / "a.wav").write_bytes(b"RIFF")
    (spk.cache_dir / "b.wav").write_bytes(b"RIFF")
    res = await spk.m_clear_cache()
    assert res["removed"] == 2
    assert spk.bus.last("/speech/speech-1/state")["cache_count"] == 0


# ─── speech-to-text (transcription) ───────────────────────────────────


class FakeStt(SttServiceBase):
    """An STT service whose 'engine' is stubbed: _create_recognizer returns a
    sentinel + tallies builds, so the shared control surface (source routing,
    listen() queue bridging, one-shot/continuous, mute, partial/final, abort)
    is exercised without a real ASR backend."""

    source = "local"
    DEFAULT_MODEL = "fake-en"

    def __init__(self, proxy_id, bus) -> None:
        super().__init__(proxy_id, bus)
        self.builds = 0
        self.resets = 0

    def _ensure_model(self, model_id):
        self.models_dir.mkdir(parents=True, exist_ok=True)
        return self.models_dir

    def _create_recognizer(self, model_dir, sample_rate):
        self.builds += 1
        return {"sr": sample_rate}

    def _reset_recognizer(self, handle):
        self.resets += 1

    def _feed(self, handle, pcm):   # overridden by loop/gate fakes; default no-op
        return None

    def _list_models(self):
        return [{"id": "fake-en", "label": "Fake", "downloaded": True}]


def _stt(tmp_path, monkeypatch, cls=FakeStt) -> FakeStt:
    monkeypatch.setenv("BOTGEAR_DATA_DIR_ABS", str(tmp_path))
    return cls("stt-1", FakeBus())


@pytest.mark.asyncio
async def test_stt_listen_one_shot_returns_and_idles(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    # Timeout path: nothing enqueued → {text:'', timeout:True} (the contract
    # the brain's loop_on_timeout re-listens on).
    out = await stt.m_listen(timeout_seconds=0.5)
    assert out == {"text": "", "bearing": None, "timeout": True}
    # One-shot: the recognizer was built but listen returned it to idle.
    assert stt._recognizer is not None and stt.builds == 1
    assert stt._active is False
    # Utterance path: an enqueued final is returned, reusing the handle.
    stt._enqueue_utterance("turn left")
    out = await stt.m_listen(timeout_seconds=2)
    assert out == {"text": "turn left", "bearing": None, "timeout": False}
    assert stt.builds == 1                       # reused, not rebuilt


@pytest.mark.asyncio
async def test_stt_start_latches_active_unlike_listen(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.m_start()
    assert stt._active is True and stt._recognizer is not None
    # A listen during an explicit start session does NOT own activation, so it
    # leaves continuous mode running.
    stt._enqueue_utterance("hi")
    out = await stt.m_listen(timeout_seconds=1)
    assert out["text"] == "hi"
    assert stt._active is True
    await stt.m_stop()
    assert stt._active is False


@pytest.mark.asyncio
async def test_stt_start_stop_persist_continuous_flag(tmp_path, monkeypatch):
    """start()/stop() must persist continuous (mirrors AudioDeviceServiceBase's
    connect()/disconnect() persisting `connected`) — otherwise a subprocess
    restart has no record that the operator wanted listening on, and
    on_start()'s ``if self.config.continuous`` check silently never fires."""
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    assert stt.config.continuous is False
    await stt.m_start()
    assert stt.config.continuous is True
    assert stt.bus.last("/stt/stt-1/state")["continuous"] is True
    await stt.m_stop()
    assert stt.config.continuous is False
    assert stt.bus.last("/stt/stt-1/state")["continuous"] is False


@pytest.mark.asyncio
async def test_stt_resumes_listening_on_restart_when_continuous(tmp_path, monkeypatch):
    """The other half of the fix: on_start() already checked
    config.continuous before this change — a fresh process booting with
    that flag pre-set (as start() now persists) must come up listening on
    its own, the same recovery a mic gets from config.connected."""
    stt = _stt(tmp_path, monkeypatch)
    stt.config = stt.config.merge_dict({
        "continuous": True, "input_kind": "topic", "input_ref": "/microphone/mic-1/audio",
    })
    assert stt._active is False           # not yet started
    await stt.on_start()
    try:
        assert stt._active is True
        assert stt._recognizer is not None
        assert stt.bus.last("/stt/stt-1/state")["listening"] is True
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stt_listen_fast_fails_without_microphone(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    # No select_input → no source. listen must NOT block the full window or
    # report clean silence (which would trap the brain's loop_on_timeout); it
    # returns timeout:False + an error so the failure is distinguishable.
    t0 = time.perf_counter()
    out = await stt.m_listen(timeout_seconds=5)
    assert time.perf_counter() - t0 < 1.0        # did not block 5s
    assert out["timeout"] is False
    assert "microphone" in (out.get("error") or "").lower()
    assert stt._recognizer is None and stt.builds == 0


@pytest.mark.asyncio
async def test_stt_listen_fast_fails_on_build_failure(tmp_path, monkeypatch):
    class BoomStt(FakeStt):
        def _create_recognizer(self, model_dir, sample_rate):
            raise RuntimeError("model boom")
    stt = _stt(tmp_path, monkeypatch, cls=BoomStt)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    out = await stt.m_listen(timeout_seconds=5)
    assert out["timeout"] is False and "boom" in out["error"]
    assert stt._recognizer is None


@pytest.mark.asyncio
async def test_stt_handle_result_partial_then_final(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt._handle_result({"text": "hel", "final": False})
    await stt._handle_result({"text": "hel", "final": False})   # unchanged → no dup publish
    await stt._handle_result({"text": "hello there", "final": True})
    texts = [(p["text"], p["final"]) for t, p, _ in stt.bus.published if t.endswith("/text")]
    assert texts == [("hel", False), ("hello there", True)]     # partial published once, then final
    # Only the final is enqueued for listen().
    assert stt._queue.qsize() == 1
    assert stt._queue.get_nowait()["text"] == "hello there"


@pytest.mark.asyncio
async def test_stt_empty_final_publishes_terminal(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt._handle_result({"text": "", "final": True})
    texts = [(p["text"], p["final"]) for t, p, _ in stt.bus.published if t.endswith("/text")]
    assert texts == [("", True)]                 # terminal published so a UI clears the partial
    assert stt._queue.qsize() == 0               # empty final is NOT a returnable utterance


@pytest.mark.asyncio
async def test_stt_recognizer_loop_end_to_end(tmp_path, monkeypatch):
    class LoopStt(FakeStt):
        def _feed(self, handle, pcm):
            return {"text": "go forward", "final": True}
    stt = _stt(tmp_path, monkeypatch, cls=LoopStt)
    await stt.on_start()
    try:
        await stt.m_select_input("topic", "/microphone/mic-1/audio")
        await stt.m_start()                      # latch active so the loop keeps running
        frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 9, 9))
        await stt._on_topic_frame(frame, "/microphone/mic-1/audio")
        out = await stt.m_listen(timeout_seconds=2)   # waits for the loop to enqueue
        assert out == {"text": "go forward", "bearing": None, "timeout": False}
        finals = [p for t, p, _ in stt.bus.published if t.endswith("/text") and p.get("final")]
        assert finals and finals[-1]["text"] == "go forward"
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stt_recognizer_loop_surfaces_feed_error(tmp_path, monkeypatch):
    """A _feed() that raises on every call (e.g. a missing runtime dep —
    exactly what a stt_local venv without numpy did) must not fail silently
    forever: frames keep arriving, ready/listening stay true, but nothing is
    ever transcribed with zero visible signal. The loop should surface it via
    last_error instead of only logging."""
    class BoomFeedStt(FakeStt):
        def _feed(self, handle, pcm):
            raise ModuleNotFoundError("No module named 'numpy'")
    stt = _stt(tmp_path, monkeypatch, cls=BoomFeedStt)
    await stt.on_start()
    try:
        await stt.m_select_input("topic", "/microphone/mic-1/audio")
        await stt.m_start()
        frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 9, 9))
        await stt._on_topic_frame(frame, "/microphone/mic-1/audio")
        # Give the loop a moment to pick the frame up and feed it.
        for _ in range(50):
            if stt._last_error:
                break
            await asyncio.sleep(0.02)
        assert stt._last_error is not None
        assert "numpy" in stt._last_error
        states = [p for t, p, _ in stt.bus.published if t.endswith("/state")]
        assert states and states[-1]["last_error"] == stt._last_error
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stt_gen_gate_drops_result_after_stop(tmp_path, monkeypatch):
    # A stop landing while a feed is in flight must invalidate that result so
    # the next listen() can't return a ghost utterance from the stopped session.
    class GateStt(FakeStt):
        def __init__(self, proxy_id, bus) -> None:
            super().__init__(proxy_id, bus)
            self.feeding = threading.Event()
            self.release = threading.Event()
        def _feed(self, handle, pcm):
            self.feeding.set()
            self.release.wait(timeout=2)
            return {"text": "stale", "final": True}
    stt = _stt(tmp_path, monkeypatch, cls=GateStt)
    await stt.on_start()
    try:
        await stt.m_select_input("topic", "/microphone/mic-1/audio")
        await stt.m_start()
        frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 9, 9))
        await stt._on_topic_frame(frame, "/microphone/mic-1/audio")
        await asyncio.to_thread(stt.feeding.wait, 2)   # loop is now inside _feed
        await stt.m_stop()                             # bumps _gen, drains, _active False
        stt.release.set()                              # feed returns the now-stale result
        await asyncio.sleep(0.15)                      # let the loop run the gen-gate
        assert stt._queue.qsize() == 0                 # stale final dropped, not enqueued
    finally:
        stt.release.set()
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stt_select_input_subscribes_gates_and_drains(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    assert stt.config.input_kind == "topic"
    assert "/microphone/mic-1/audio" in stt._subscribed_topics
    with pytest.raises(Exception):                              # only 'topic' (or None) allowed
        await stt.m_select_input("carrier-pigeon", "x")
    # A finalized utterance from the old source is dropped when the source switches.
    stt._enqueue_utterance("from mic-1")
    await stt.m_select_input("topic", "/microphone/mic-2/audio")
    assert stt._queue.qsize() == 0
    # Frames only ingest when active + topic matches.
    frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 9, 9))
    await stt._on_topic_frame(frame, "/microphone/mic-2/audio")  # inactive → dropped
    assert stt._frame_q.qsize() == 0
    await stt._activate()
    await stt._on_topic_frame(frame, "/microphone/other/audio")  # wrong topic → dropped
    assert stt._frame_q.qsize() == 0
    await stt._on_topic_frame(frame, "/microphone/mic-2/audio")  # match → queued
    assert stt._frame_q.qsize() == 1


@pytest.mark.asyncio
async def test_stt_mute_drops_frames_and_resets(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt._activate()
    # Seed mid-stream state, then mute must drain audio + utterances + reset.
    stt._frame_q.put_nowait(b"\x00\x00")
    stt._enqueue_utterance("pre-mute")
    stt._last_partial = "hel"
    stt._level = 0.5
    await stt.m_mute()
    assert stt._frame_q.qsize() == 0 and stt._queue.qsize() == 0
    state = stt.bus.last("/stt/stt-1/state")
    assert state["muted"] is True and state["last_partial"] == "" and state["level_rms"] == 0.0
    # Future frames stay dropped while muted.
    frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 9, 9))
    await stt._on_topic_frame(frame, "/microphone/mic-1/audio")
    assert stt._frame_q.qsize() == 0


@pytest.mark.asyncio
async def test_stt_set_model_rebuilds_only_when_active(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt._activate()
    assert stt.builds == 1
    await stt.m_set_model("other")
    assert stt._resolve_model() == "other"
    assert stt.builds == 2 and stt._recognizer is not None       # rebuilt live
    # On an inactive service, set_model only flags a rebuild — no eager build.
    stt2 = _stt(tmp_path, monkeypatch)
    await stt2.m_set_model("x")
    assert stt2.builds == 0 and stt2._recognizer is None


@pytest.mark.asyncio
async def test_stt_stop_clears_queues_and_bumps_gen(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt._activate()
    stt._enqueue_utterance("one")
    stt._frame_q.put_nowait(b"\x00\x00")
    gen0 = stt._gen
    await stt.m_stop()
    assert stt._gen == gen0 + 1
    assert stt._active is False
    assert stt._queue.qsize() == 0 and stt._frame_q.qsize() == 0
    assert stt.resets == 1                       # stream reset on stop
    assert stt.bus.last("/stt/stt-1/state")["listening"] is False


@pytest.mark.asyncio
async def test_stt_meta_topics_and_default_model(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    assert stt.meta_topics()["text"].endswith("/stt/stt-1/text")
    assert stt._resolve_model() == "fake-en"                    # falls back to DEFAULT_MODEL
    await stt.m_set_model("other")
    assert stt._resolve_model() == "other"


# ─── transcript-to-file (save-to-disk) ─────────────────────────────────


@pytest.mark.asyncio
async def test_transcript_recording_writes_finals_only(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.on_start()
    try:
        out = await stt.m_start_recording("session.txt")
        state = stt.bus.last("/stt/stt-1/state")
        assert state["recording_transcript"] is True
        assert state["transcript_path"].endswith("session.txt")
        # Partials must NOT be written — only non-empty finals.
        await stt._handle_result({"text": "hel", "final": False})
        await stt._handle_result({"text": "hello there", "final": True})
        await stt._handle_result({"text": "", "final": True})   # empty final: no line
        res = await stt.m_stop_recording()
        assert res["lines"] == 1
        path = tmp_path / "transcripts" / "session.txt"
        assert path.is_file()
        lines = path.read_text().splitlines()
        assert len(lines) == 1
        assert lines[0].endswith("hello there")
        assert lines[0].startswith("[")              # timestamp prefix
        # A fresh suggestion is pre-loaded for next time.
        assert stt.bus.last("/stt/stt-1/state")["transcript_suggested_path"].endswith(".txt")
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_transcript_recording_appends_across_start_stop_cycles(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.on_start()
    try:
        await stt.m_start_recording("session.txt")
        await stt._handle_result({"text": "first", "final": True})
        await stt.m_stop_recording()
        await stt.m_start_recording("session.txt")   # same file, second session
        await stt._handle_result({"text": "second", "final": True})
        await stt.m_stop_recording()
        path = tmp_path / "transcripts" / "session.txt"
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0].endswith("first") and lines[1].endswith("second")
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_transcript_suggested_path_under_transcripts_dir(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.on_start()
    try:
        p = stt._fresh_transcript_path()
        assert p.startswith(str(tmp_path / "transcripts"))
        assert p.endswith(".txt")
        assert "stt-1" in p
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stop_transcript_recording_without_start_is_a_noop(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    res = await stt.m_stop_recording()
    assert res == {"path": None, "lines": 0}
    assert stt._recording_transcript is False


# ─── speak(wait=True) + speakable text ────────────────────────────────


@pytest.mark.asyncio
async def test_speech_speak_wait_returns_after_streaming(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    await spk.on_start()
    try:
        res = await spk.m_speak("hello there", wait=True, wait_timeout_s=5)
        assert res["finished"] is True and res["outcome"] == "finished"
        # Streaming is over by the time speak returned: frames were sent and
        # the service is idle again.
        assert any(t.endswith("/audio") for t, _, _ in spk.bus.published)
        assert spk._speaking is False and spk._pending == []
    finally:
        await spk.on_stop()


@pytest.mark.asyncio
async def test_speech_speak_without_wait_returns_immediately(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    res = await spk.m_speak("hello there")      # no worker running → still returns
    assert "finished" not in res and res["queued"] == 1


@pytest.mark.asyncio
async def test_speech_speak_wait_times_out_without_blocking(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)         # no worker → never streams
    res = await spk.m_speak("hello", wait=True, wait_timeout_s=0.2)
    assert res["finished"] is False and res["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_speech_speak_wait_resolves_when_interrupted(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)         # no worker → item stays queued
    waiter = asyncio.create_task(spk.m_speak("first", wait=True, wait_timeout_s=5))
    await asyncio.sleep(0.05)
    await spk.m_stop()
    res = await asyncio.wait_for(waiter, 2)
    assert res["finished"] is False and res["outcome"] == "interrupted"


@pytest.mark.asyncio
async def test_speech_state_carries_proxy_id(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    await spk.m_speak("hi")
    assert spk.bus.last("/speech/speech-1/state")["proxy_id"] == "speech-1"


@pytest.mark.asyncio
async def test_speech_speak_strips_markdown(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    res = await spk.m_speak("Options:\n\n1. **Device Status**\n- *Audio* settings")
    assert res["spoken"] == "Options: 1. Device Status. Audio settings."


@pytest.mark.parametrize("raw, spoken", [
    ("Hello there", "Hello there"),
    ("Done!", "Done!"),
    ("# Heading\nBody text", "Heading. Body text."),
    ("Let me clarify the options:\n\n1. **Device Status** (e.g., switches)\n2. **Hardware Control**",
     "Let me clarify the options: 1. Device Status (e.g., switches). 2. Hardware Control."),
    ("- one\n- two\n* three", "one. two. three."),
    ("see [the docs](http://x) and `run_it`", "see the docs and run_it"),
    ("| a | b |\n|---|---|\n| 1 | 2 |", "a, b. 1, 2."),
    ("keep snake_case_names and 2 * 3", "keep snake_case_names and 2 * 3"),
    ("> quoted\n---\nafter", "quoted. after."),
    ("", ""),
])
def test_speakable_text(raw, spoken):
    from rlx_audio.base import speakable_text
    assert speakable_text(raw) == spoken


# ─── STT echo gate ─────────────────────────────────────────────────────


def _speech_state(**kw) -> Dict[str, Any]:
    state = {"proxy_id": "mouth", "speaking": False, "synthesizing": False, "queue": []}
    state.update(kw)
    return state


@pytest.mark.asyncio
async def test_stt_echo_gate_drops_frames_while_speech_busy(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.m_start()
    frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 9, 9))

    for busy in ({"synthesizing": True}, {"speaking": True}, {"queue": ["next"]}):
        await stt._on_speech_state(_speech_state(**busy))
        await stt._on_topic_frame(frame, "/microphone/mic-1/audio")
        assert stt._frame_q.qsize() == 0, busy
        assert stt.bus.last("/stt/stt-1/state")["echo_suppressed"] is True


@pytest.mark.asyncio
async def test_stt_echo_gate_reopens_after_holdoff(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    stt.config.echo_holdoff_ms = 100
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.m_start()
    frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 9, 9))

    await stt._on_speech_state(_speech_state(speaking=True))
    await stt._on_speech_state(_speech_state())          # speech ended
    await stt._on_topic_frame(frame, "/microphone/mic-1/audio")
    assert stt._frame_q.qsize() == 0                     # still inside the hold-off
    await asyncio.sleep(0.15)
    await stt._on_topic_frame(frame, "/microphone/mic-1/audio")
    assert stt._frame_q.qsize() == 1                     # gate open again
    assert stt._echo_gated() is False


@pytest.mark.asyncio
async def test_stt_echo_gate_tracks_each_speech_service(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    stt.config.echo_holdoff_ms = 0
    await stt._on_speech_state(_speech_state(proxy_id="a", speaking=True))
    await stt._on_speech_state(_speech_state(proxy_id="b", speaking=True))
    await stt._on_speech_state(_speech_state(proxy_id="a"))
    assert stt._echo_gated() is True                     # b is still talking
    await stt._on_speech_state(_speech_state(proxy_id="b"))
    assert stt._echo_gated() is False


@pytest.mark.asyncio
async def test_stt_echo_gate_ignores_stale_busy_report(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt._on_speech_state(_speech_state(speaking=True))
    stt._speech_busy["mouth"] -= stt._ECHO_STALE_S + 1   # speech died mid-utterance
    assert stt._echo_gated() is False


@pytest.mark.asyncio
async def test_stt_echo_gate_discards_results_and_partial_but_keeps_earlier_utterances(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.m_start()
    await stt._handle_result({"text": "turn on the light", "final": True})   # operator, before speech
    await stt._handle_result({"text": "I'll", "final": False})              # partial in flight
    gen0 = stt._gen

    await stt._on_speech_state(_speech_state(speaking=True))
    assert stt._gen == gen0 + 1                          # in-flight feeds invalidated
    assert stt.resets >= 1                               # decode state reset
    assert stt._last_partial == ""
    await stt._handle_result({"text": "I'll turn on the light", "final": True})   # echo
    assert [stt._queue.get_nowait()["text"] for _ in range(stt._queue.qsize())] == ["turn on the light"]


@pytest.mark.asyncio
async def test_stt_echo_gate_can_be_disabled(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    stt.config.suppress_while_speaking = False
    await stt._on_speech_state(_speech_state(speaking=True))
    assert stt._echo_gated() is False


@pytest.mark.asyncio
async def test_stt_listen_times_out_while_speech_is_playing(tmp_path, monkeypatch):
    class EchoStt(FakeStt):
        def _feed(self, handle, pcm):
            return {"text": "echo of my own voice", "final": True}
    stt = _stt(tmp_path, monkeypatch, cls=EchoStt)
    await stt.on_start()
    try:
        await stt.m_select_input("topic", "/microphone/mic-1/audio")
        await stt.m_start()
        await stt._on_speech_state(_speech_state(speaking=True))
        frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 9, 9))
        await stt._on_topic_frame(frame, "/microphone/mic-1/audio")
        out = await stt.m_listen(timeout_seconds=0.5)
        assert out["timeout"] is True and out["text"] == ""
    finally:
        await stt.on_stop()


# ─── runtime-wide HALT ────────────────────────────────────────────────


HALT = {"level": "stop", "source": "test", "reason": ""}


@pytest.mark.asyncio
async def test_speech_on_halt_interrupts_and_clears_queue(tmp_path, monkeypatch):
    spk = _speech(tmp_path, monkeypatch)
    waiter = asyncio.create_task(spk.m_speak("one", wait=True, wait_timeout_s=5))
    await spk.m_speak("two")
    await asyncio.sleep(0.01)
    gen0 = spk._gen
    await spk.on_halt(HALT)
    assert spk._gen == gen0 + 1 and spk._pending == []
    assert (await asyncio.wait_for(waiter, 2))["outcome"] == "interrupted"


@pytest.mark.asyncio
async def test_speaker_on_halt_flushes_and_drops_in_flight_frames():
    spk = _spk()
    await spk.m_connect()
    frame = encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", 1, 2))
    await spk.on_halt(HALT)
    await spk._on_sink_frame(frame)             # in flight from before the halt → dropped
    assert spk.played == []
    spk._halt_drain_until = 0.0                 # drain window over
    await spk._on_sink_frame(frame)
    assert len(spk.played) == 1                 # speaker still works after a halt


@pytest.mark.asyncio
async def test_stt_on_halt_drops_queued_utterances_and_stop_word_tail(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.m_start()
    stt._enqueue_utterance("I DIDN'T CATCH THAT")
    await stt.on_halt(HALT)
    assert stt._queue.qsize() == 0
    await stt._handle_result({"text": "stop", "final": True})    # the stop word finalizing late
    assert stt._queue.qsize() == 0
    stt._halt_quiet_until = 0.0
    await stt._handle_result({"text": "hello", "final": True})
    assert stt._queue.qsize() == 1                               # back to normal


class SpotStt(FakeStt):
    """FakeStt with a stub spotter: any frame whose first sample is 7
    'contains' the label in ``self.hit``."""

    hit = "stop_0"

    def _create_kws(self, phrases, sample_rate):
        self.phrases = phrases
        return {"ok": True}

    def _kws_feed(self, handle, pcm):
        return self.hit if struct.unpack("<h", pcm[:2])[0] == 7 else None


async def _spot(tmp_path, monkeypatch, **cfg) -> SpotStt:
    stt = _stt(tmp_path, monkeypatch, cls=SpotStt)
    for k, v in cfg.items():
        setattr(stt.config, k, v)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.on_start()
    await stt._kws_build_task
    return stt


async def _say(stt, first_sample: int) -> None:
    await stt._on_topic_frame(encode_frame(1, 0.0, 16000, 1, struct.pack("<2h", first_sample, 0)),
                              "/microphone/mic-1/audio")
    for _ in range(50):
        await asyncio.sleep(0.01)
        if stt._kws_q.qsize() == 0:
            break
    await asyncio.sleep(0.02)


def _halts(stt) -> List[Dict[str, Any]]:
    return [p for t, p, _ in stt.bus.published if t == "/runtime/halt"]


@pytest.mark.asyncio
async def test_stop_word_broadcasts_halt_even_when_idle(tmp_path, monkeypatch):
    stt = await _spot(tmp_path, monkeypatch)
    try:
        assert stt._active is False                       # one-shot STT between turns
        assert stt.phrases == {"stop_0": "stop", "stop_1": "be quiet", "stop_2": "stop talking",
                               "emergency_0": "emergency stop"}
        await _say(stt, 7)
        halts = _halts(stt)
        assert len(halts) == 1
        assert halts[0]["level"] == "stop" and halts[0]["source"] == "voice:stt-1"
        assert stt.bus.last("/stt/stt-1/stop_word")["phrase"] == "stop"
        assert stt.bus.last("/stt/stt-1/state")["last_stop_word"] == "stop"
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_emergency_word_halts_at_emergency_level(tmp_path, monkeypatch):
    stt = await _spot(tmp_path, monkeypatch)
    stt.hit = "emergency_0"
    try:
        await _say(stt, 7)
        assert _halts(stt)[0]["level"] == "emergency"
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stop_word_heard_while_robot_talks(tmp_path, monkeypatch):
    stt = await _spot(tmp_path, monkeypatch)
    try:
        await stt._on_speech_state(_speech_state(speaking=True, current_text="Here are the options for you"))
        assert stt._echo_gated() is True                  # transcription is gated...
        await _say(stt, 7)
        assert len(_halts(stt)) == 1                      # ...but the spotter still hears "stop"
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stop_word_the_robot_itself_says_is_vetoed(tmp_path, monkeypatch):
    stt = await _spot(tmp_path, monkeypatch)
    try:
        await stt._on_speech_state(_speech_state(speaking=True, current_text="I will stop the motor now."))
        await _say(stt, 7)
        assert _halts(stt) == []
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stop_word_cooldown(tmp_path, monkeypatch):
    stt = await _spot(tmp_path, monkeypatch)
    try:
        await _say(stt, 7)
        await _say(stt, 7)                                 # "stop, stop" → one HALT
        assert len(_halts(stt)) == 1
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_stop_words_disabled(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch, cls=SpotStt)
    stt.config.stop_words_enabled = False
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.on_start()
    try:
        assert stt._kws_task is None and stt._kws is None
        await _say(stt, 7)
        assert _halts(stt) == []
    finally:
        await stt.on_stop()


@pytest.mark.asyncio
async def test_spotter_build_failure_leaves_transcription_working(tmp_path, monkeypatch):
    class BrokenSpot(FakeStt):
        def _create_kws(self, phrases, sample_rate):
            raise RuntimeError("no network")
    stt = _stt(tmp_path, monkeypatch, cls=BrokenSpot)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.on_start()
    try:
        await stt._kws_build_task
        assert stt._kws is None and "no network" in stt._kws_error
        assert stt.bus.last("/stt/stt-1/state")["stop_words_ready"] is False
        stt._enqueue_utterance("still works")
        assert (await stt.m_listen(timeout_seconds=1))["text"] == "still works"
    finally:
        await stt.on_stop()


# ─── stale utterances ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listen_skips_stale_utterances(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.m_start()                                  # continuous: queue fills with nobody listening
    stt._enqueue_utterance("turn switch one on")
    stt._queue._queue[-1]["ts"] -= 60                    # said a minute ago
    stt._enqueue_utterance("what voices do you have")
    out = await stt.m_listen(timeout_seconds=1)
    assert out["text"] == "what voices do you have"


@pytest.mark.asyncio
async def test_listen_times_out_when_only_stale_utterances(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.m_start()
    stt._enqueue_utterance("turn switch one on")
    stt._queue._queue[-1]["ts"] -= 60
    out = await stt.m_listen(timeout_seconds=0.5)
    assert out["timeout"] is True and out["text"] == ""


@pytest.mark.asyncio
async def test_listen_age_limit_can_be_disabled(tmp_path, monkeypatch):
    stt = _stt(tmp_path, monkeypatch)
    stt.config.max_utterance_age_s = 0
    await stt.m_select_input("topic", "/microphone/mic-1/audio")
    await stt.m_start()
    stt._enqueue_utterance("old but wanted")
    stt._queue._queue[-1]["ts"] -= 600
    assert (await stt.m_listen(timeout_seconds=1))["text"] == "old but wanted"
