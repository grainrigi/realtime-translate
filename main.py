import asyncio
import os
import pathlib
import queue
import threading

import pyaudio
from aiohttp import web
from dotenv import load_dotenv

from translator import CHUNK_SIZE, SAMPLE_RATE, STTTranslator, TranslationMessage

load_dotenv()


def list_input_devices() -> list[dict]:
    pa = pyaudio.PyAudio()
    devices = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            devices.append({"index": i, "name": info["name"], "rate": int(info["defaultSampleRate"])})
    pa.terminate()
    return devices


def select_device() -> int:
    devices = list_input_devices()
    if not devices:
        raise RuntimeError("No input devices found")

    print("\n=== Available input devices ===")
    for i, d in enumerate(devices):
        print(f"  [{i}] {d['name']}  (device_index={d['index']}, rate={d['rate']})")
    print()

    while True:
        try:
            choice = input(f"Select device [0-{len(devices)-1}]: ").strip()
            idx = int(choice)
            if 0 <= idx < len(devices):
                selected = devices[idx]
                print(f"  -> Using: {selected['name']}\n")
                return selected["index"]
        except (ValueError, EOFError):
            pass
        print("  Invalid choice, try again.")


def capture_audio(stt: STTTranslator, device_index: int):
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=CHUNK_SIZE,
    )
    try:
        while stt.running:
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            stt.feed_audio(data)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


async def handle_index(request):
    html_path = pathlib.Path(__file__).parent / "index.html"
    return web.FileResponse(html_path)


async def handle_websocket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    device_index = request.app["device_index"]
    ws_queue: queue.Queue[str] = queue.Queue()

    def on_result(msg: TranslationMessage):
        ws_queue.put(msg.to_json())

    creds_path = os.path.abspath(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    stt = STTTranslator(on_result=on_result, credentials_path=creds_path)

    audio_thread = threading.Thread(target=capture_audio, args=(stt, device_index), daemon=True)
    audio_thread.start()
    stt.run_in_thread()

    print("WebSocket client connected. Listening...")

    try:
        while not ws.closed:
            try:
                msg = ws_queue.get(timeout=0.05)
                await ws.send_str(msg)
            except queue.Empty:
                await asyncio.sleep(0.01)
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        stt.stop()
        print("WebSocket client disconnected.")

    return ws


async def main():
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path or not os.path.exists(creds_path):
        print("ERROR: Set GOOGLE_APPLICATION_CREDENTIALS in .env to your service account key JSON path")
        return

    device_index = select_device()

    app = web.Application()
    app["device_index"] = device_index
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_websocket)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 8080)
    await site.start()

    print("Server running at http://localhost:8080")
    print("Open the URL in your browser to start real-time translation.")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
