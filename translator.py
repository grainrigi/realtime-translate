import html as html_mod
import json
import math
import queue
import re
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator

from google.cloud import speech
from google.cloud import translate_v2 as translate
from google.oauth2 import service_account

SAMPLE_RATE = 16000
CHUNK_DURATION_MS = 100
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)
SILENCE_THRESHOLD_RMS = 500
STREAM_TIME_LIMIT = 300  # 5min: forced cut
SENTENCE_ENDINGS_PATTERN = re.compile(r"[.?!]")
TRANSLATE_INTERVAL = 0.5


def calculate_rms(data: bytes) -> float:
    count = len(data) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"<{count}h", data)
    sum_squares = sum(s * s for s in shorts)
    return math.sqrt(sum_squares / count)


class SilenceDetector:
    """Adaptive silence detector with escalating sensitivity based on stream age.

    - < 3min: no silence cut
    - 3-4min: cut on 0.7s silence
    - 4-5min: cut on 0.3s silence
    - >= 5min: forced cut (handled in audio generator)
    """

    TIERS = [
        (180, None),   # < 3min: no cut
        (240, 0.7),    # 3-4min: 0.7s silence
        (300, 0.3),    # 4-5min: 0.3s silence
    ]

    def __init__(self, threshold: float = SILENCE_THRESHOLD_RMS):
        self.threshold = threshold
        self.silence_start: float | None = None

    def feed(self, rms: float, now: float, stream_start: float) -> bool:
        stream_age = now - stream_start

        required_silence: float | None = None
        matched = False
        for age_limit, duration in self.TIERS:
            if stream_age < age_limit:
                required_silence = duration
                matched = True
                break
        if not matched:
            required_silence = 0.3

        if required_silence is None:
            self.silence_start = None
            return False

        if rms < self.threshold:
            if self.silence_start is None:
                self.silence_start = now
            if (now - self.silence_start) >= required_silence:
                return True
        else:
            self.silence_start = None
        return False

    def reset(self):
        self.silence_start = None


def load_credentials(credentials_path: str) -> service_account.Credentials:
    return service_account.Credentials.from_service_account_file(credentials_path)


class TranslationClient:
    def __init__(self, credentials_path: str):
        creds = load_credentials(credentials_path)
        self.client = translate.Client(credentials=creds)

    def translate(self, text: str) -> str:
        if not text.strip():
            return ""
        result = self.client.translate(text, target_language="ja", source_language="en")
        return html_mod.unescape(result["translatedText"])


class TranslationMessage:
    def __init__(self, msg_type: str, message_id: str, original: str, translated: str):
        self.type = msg_type
        self.message_id = message_id
        self.original = original
        self.translated = translated

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type,
            "message_id": self.message_id,
            "original": self.original,
            "translated": self.translated,
        })


OnResult = Callable[[TranslationMessage], None]


class STTTranslator:
    """Core STT + Translation engine.

    STT layer: manages streaming sessions, cuts on silence. Accumulates transcript
    across stream boundaries.

    Translation layer: independent of STT streams. Segments messages by sentence-ending
    punctuation (.?!) only.
    """

    def __init__(self, on_result: OnResult, credentials_path: str):
        creds = load_credentials(credentials_path)
        self.speech_client = speech.SpeechClient(credentials=creds)
        self.translator_client = TranslationClient(credentials_path)
        self.on_result = on_result
        self.audio_queue: queue.Queue[tuple[bytes, float] | None] = queue.Queue()
        self.running = True

        # STT state: accumulates across stream restarts
        self.confirmed_text = ""  # all is_final text from all streams
        self.current_interim = ""  # latest interim from current stream

        # Translation state: independent of STT streams
        self.finalized_text = ""  # text already emitted as finalized messages
        self.current_message_id = str(uuid.uuid4())

        self.silence_detector = SilenceDetector()
        self.stream_start_time = 0.0
        self._should_cut = False
        self._last_translate_time = 0.0

    @property
    def full_text(self) -> str:
        if self.confirmed_text and self.current_interim:
            return self.confirmed_text + " " + self.current_interim
        return self.confirmed_text + self.current_interim

    def feed_audio(self, chunk: bytes):
        rms = calculate_rms(chunk)
        self.audio_queue.put((chunk, rms))

    def feed_done(self):
        self.audio_queue.put(None)

    def stop(self):
        self.running = False
        self.audio_queue.put(None)

    def run(self):
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE,
            language_code="en-US",
            enable_automatic_punctuation=True,
        )
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,
        )

        stream_count = 0
        while self.running:
            stream_count += 1
            self.stream_start_time = time.time()
            self.silence_detector.reset()
            self._should_cut = False
            self._audio_ended = False
            self.current_interim = ""
            confirmed_len = len(self.confirmed_text)
            print(f"[STT] Stream #{stream_count} started (confirmed={confirmed_len} chars)")

            try:
                responses = self.speech_client.streaming_recognize(
                    config=streaming_config,
                    requests=self._audio_generator(),
                )
                for response in responses:
                    if not self.running:
                        break
                    self._process_response(response)
            except Exception as e:
                if self.running:
                    print(f"STT stream error: {e}")
                    if "403" in str(e) or "SERVICE_DISABLED" in str(e):
                        print("Fatal: API not enabled. Stopping.")
                        self.running = False
                        break
                    time.sleep(0.5)

            if self._audio_ended:
                break

    def run_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True)
        t.start()
        return t

    def _audio_generator(self) -> Iterator[speech.StreamingRecognizeRequest]:
        while self.running and not self._should_cut:
            try:
                item = self.audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if item is None:
                self._audio_ended = True
                break

            data, rms = item
            now = time.time()

            stream_age = now - self.stream_start_time

            if self.silence_detector.feed(rms, now, self.stream_start_time):
                self._should_cut = True
                print(f"[STT] Silence cut at {stream_age:.1f}s")
                yield speech.StreamingRecognizeRequest(audio_content=data)
                break

            if stream_age > STREAM_TIME_LIMIT:
                self._should_cut = True
                print(f"[STT] Forced cut at {stream_age:.1f}s (time limit)")
                yield speech.StreamingRecognizeRequest(audio_content=data)
                break

            yield speech.StreamingRecognizeRequest(audio_content=data)

    # --- STT response handling ---

    def _process_response(self, response):
        for result in response.results:
            if not result.alternatives:
                continue
            transcript = result.alternatives[0].transcript
            if result.is_final:
                if self.confirmed_text and not self.confirmed_text.endswith(" "):
                    self.confirmed_text += " "
                self.confirmed_text += transcript.strip()
                self.current_interim = ""
                self._update_translation(is_final=True)
            else:
                self.current_interim = transcript
                self._update_translation(is_final=False)

    # --- Translation layer (independent of STT stream boundaries) ---

    def _update_translation(self, is_final: bool):
        pending = self.full_text[len(self.finalized_text):]
        if not pending.strip():
            return

        now = time.time()
        if not is_final and (now - self._last_translate_time) < TRANSLATE_INTERVAL:
            return

        # Find the last sentence-ending punctuation in pending text
        last_split = -1
        for m in SENTENCE_ENDINGS_PATTERN.finditer(pending):
            last_split = m.end()

        if last_split > 0 and is_final:
            # Finalize everything up to the last sentence ending
            to_finalize = pending[:last_split].strip()
            remainder = pending[last_split:].strip()

            if to_finalize:
                self._last_translate_time = now
                try:
                    translated = self.translator_client.translate(to_finalize)
                except Exception as e:
                    print(f"Translation error: {e}")
                    translated = "(translation error)"

                self.on_result(TranslationMessage(
                    msg_type="finalize",
                    message_id=self.current_message_id,
                    original=to_finalize,
                    translated=translated,
                ))

                self.finalized_text = self.full_text[:len(self.finalized_text) + last_split]
                self.current_message_id = str(uuid.uuid4())

            if remainder:
                try:
                    rest_translated = self.translator_client.translate(remainder)
                except Exception:
                    rest_translated = ""
                self.on_result(TranslationMessage(
                    msg_type="update",
                    message_id=self.current_message_id,
                    original=remainder,
                    translated=rest_translated,
                ))
        else:
            # No sentence ending (or interim): update current message
            self._last_translate_time = now
            try:
                translated = self.translator_client.translate(pending)
            except Exception as e:
                print(f"Translation error: {e}")
                translated = "(translation error)"

            self.on_result(TranslationMessage(
                msg_type="update",
                message_id=self.current_message_id,
                original=pending,
                translated=translated,
            ))
