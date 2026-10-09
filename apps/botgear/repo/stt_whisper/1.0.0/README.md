# stt_whisper — Whisper-family speech-to-text

Second `transcription` implementor alongside `stt_local` (sherpa-onnx).
Subprocess service, own venv (`faster-whisper` / CTranslate2, no torch).
Uses the same shared base as every STT engine —
`rlx_audio.SttServiceBase` — implementing only the four engine hooks
(`_ensure_model`, `_create_recognizer`, `_feed`, `_list_models`).

```
type:      stt_whisper
implements: [transcription]
source:    "whisper"   (SttConfig._snapshot() field — lets a UI/consumer
                         tell engines apart without parsing proxy_id)
```

**Does not override `type_name`** — stays `"stt"` (inherited from
`SttServiceBase`), same as `stt_local`. This is *the* mechanism that
makes transcription engines swappable: both publish under
`/stt/<proxy_id>/...`, differentiated only by instance id, not a
per-engine topic namespace. Any new STT engine must follow this
convention.


## Key behavioral difference from stt_local

Whisper is not a streaming recognizer — it transcribes a whole audio
buffer in one forward pass, so `_feed` buffers frames internally and
does its own RMS-based silence/endpoint detection (`endpoint_silence_ms`,
reused from `SttConfig`, not a new knob). It only calls the model once
per utterance-end, and **only ever emits `final: true`** — `last_partial`
stays permanently empty. `/stt/{id}/text` consumers relying on live
partials (stt_local's behavior) will not get them from this engine.
`max_utterance_s` (default 30s) is a hard flush cap so a stuck VAD can't
grow the buffer unboundedly.


## Gotchas

- **No 32-bit ARM.** `ctranslate2` publishes no 32-bit ARM wheels
  (unlike `stt_local`'s sherpa-onnx, which supports armv7l) — only
  64-bit Raspberry Pi OS is supported.
- **Model cache** lives under `<data_dir>/stt/models/`, resolved via
  `WhisperModel(download_root=...)`, not huggingface_hub's default
  `~/.cache/huggingface`.
- **CPU inference** uses `compute_type="int8"` (CTranslate2 quantization)
  by default — same "offline, no GPU required" posture as every other
  botgear audio service; `device`/`compute_type` are exposed but GPU
  install-time branching is not implemented.
- **First utterance after a slow transcribe can be lost**: `_feed` runs
  serialized on `SttServiceBase`'s single-worker executor, so frames
  landing while a multi-second Whisper call is in flight queue up
  (bounded ~2s) rather than being dropped immediately, but a very slow
  inference on constrained hardware (e.g. Raspberry Pi with `small`/
  `medium`) can still lose the start of a new utterance.
