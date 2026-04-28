"""Test script: stream test_30s.wav through STT + Translation as if it were real-time mic input."""

import os
import sys
import time
import wave

from dotenv import load_dotenv

from translator import CHUNK_DURATION_MS, CHUNK_SIZE, SAMPLE_RATE, STTTranslator, TranslationMessage

load_dotenv()

AUDIO_FILE = os.path.join(os.path.dirname(__file__), "test_5m.wav")


def on_result(msg: TranslationMessage):
    marker = ">>>" if msg.type == "finalize" else "..."
    print(f"[{marker}] ({msg.message_id[:8]}) {msg.original}")
    print(f"       -> {msg.translated}")
    print()
    sys.stdout.flush()


def main():
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path or not os.path.exists(creds_path):
        print("ERROR: Set GOOGLE_APPLICATION_CREDENTIALS in .env")
        sys.exit(1)

    print(f"Loading {AUDIO_FILE}...")
    with wave.open(AUDIO_FILE, "rb") as wf:
        assert wf.getsampwidth() == 2
        assert wf.getnchannels() == 1
        raw_data = wf.readframes(wf.getnframes())

    chunk_bytes = CHUNK_SIZE * 2
    total_chunks = len(raw_data) // chunk_bytes
    duration_sec = len(raw_data) / (SAMPLE_RATE * 2)

    print(f"Audio: {duration_sec:.1f}s, {total_chunks} chunks")
    print("Streaming at real-time pace...")
    print("=" * 60)
    sys.stdout.flush()

    creds_path = os.path.abspath(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    stt = STTTranslator(on_result=on_result, credentials_path=creds_path)
    stt_thread = stt.run_in_thread()

    try:
        for i in range(total_chunks):
            start = i * chunk_bytes
            chunk = raw_data[start : start + chunk_bytes]
            stt.feed_audio(chunk)
            time.sleep(CHUNK_DURATION_MS / 1000.0)

        stt.feed_done()
        print("=" * 60)
        print("Audio stream ended. Waiting for final results...")
        sys.stdout.flush()
        stt_thread.join(timeout=15)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        stt.stop()


if __name__ == "__main__":
    main()
