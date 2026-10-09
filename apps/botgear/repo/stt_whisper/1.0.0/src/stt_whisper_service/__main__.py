"""stt_whisper_service entry point. The listen/start/stop/select_input/mute
interface + recognizer loop + utterance queue live in
rlx_audio.SttServiceBase; this service implements the faster-whisper backend."""
from __future__ import annotations

import sys

from .service import SttWhisperService


def main() -> int:
    return SttWhisperService.run()


if __name__ == "__main__":
    sys.exit(main())
