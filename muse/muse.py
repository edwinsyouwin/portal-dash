#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "musegadget @ git+https://github.com/facebookincubator/muse-gadget-sdk@a5f809f621c4cbdbd37336fe17e7cdb74cebf3fe#subdirectory=linux",
#   "certifi", "websockets>=15", "openwakeword>=0.6", "onnxruntime", "numpy",
# ]
# ///
"""Muse voice for the Portal dashboard, built on Meta's Muse Gadget SDK (musegadget).

  muse.py pair     pair with the Muse app, using the Portal's Bluetooth (needs the USB cable)
  muse.py status   show whether this Mac is paired, and with which Muse
  muse.py serve    carry voice calls between the dashboard and Muse
  muse.py key      print the key the dashboard needs (muse.key in dashboard/config.js)

The SDK is written for Linux, where it advertises over the machine's own Bluetooth. Here
the Portal's radio does that instead (BleRelay in the shell app) and this Mac runs the
pairing protocol. Nothing here registers commands with Muse, so Muse cannot run anything
on this Mac. The pairing (device tokens) is kept in STATE_DIR, outside the repository.

For a call, the dashboard (voice.js) opens a WebSocket to `serve`, sends its microphone as
24 kHz 16-bit mono PCM and plays the PCM that comes back; the SDK's VoiceCall does the rest.
With the wake word on, the dashboard sends its microphone all the time and two detectors
listen to it on this Mac, starting a call when either hears its phrase: the SDK's
"Hey Muse" (build-wakeword.sh) for a voice call and openWakeWord's "Hey Jarvis" for a
video call, in which Muse's picture is shown on the Portal as well (needs libopus:
`brew install opus`). MUSE_VIDEO_PHRASE in .env moves video to the other phrase, or to
neither with any other value.
"""

import argparse
import asyncio
import hmac
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT.parent / ".env"
STATE_DIR = Path("~/.config/portal-dash/muse").expanduser()
WAKEWORD_LIBRARY = ROOT / "build" / "libmuse_wakeword.dylib"  # from build-wakeword.sh
SHELL_ACTIVITY = "com.portaldash.shell/.MainActivity"
RELAY_PORT = 8767
VOICE_PORT = 8768
KEY_FILE = "portal_key"
# Device tokens are rotated once they are this old, as the SDK's own service does.
TOKEN_REFRESH_AGE_S = 3 * 3600
# openWakeWord scores each 80 ms of sound from 0 to 1; its own suggested cutoff.
JARVIS_THRESHOLD = 0.5
# The wake phrase that starts a video call; any other starts a voice call.
VIDEO_PHRASE = "Hey Jarvis"
# First byte of each binary message to the dashboard: sound, or a video frame that is
# (K) or is not (D) a keyframe, which a decoder needs to start from.
AUDIO, KEYFRAME, DELTA = b"A", b"K", b"D"
# Video frames waiting for a slow dashboard beyond this are dropped until the next keyframe.
MAX_QUEUED_MESSAGES = 100

log = logging.getLogger("muse")

# The setup service the Muse app looks for (musegadget.ble_server, which needs Linux's
# BlueZ and so cannot be imported here).
SERVICE_UUID = "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c"
RX_UUID = "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01"
TX_UUID = "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c"
PAIRED_FLAG_COMPANY_ID = 0xFFFF


def configure():
    """Point the SDK at this project's settings. Call before using musegadget."""
    for line in ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []:
        key, separator, value = line.partition("=")
        key, value = key.strip().removeprefix("export "), value.strip().strip("\"'")
        if separator and key == "MUSE_SDK_TOKEN":
            os.environ.setdefault("MUSEGADGET_SDK_TOKEN", value)
        elif separator and key in ("MUSE_VIDEO_PHRASE", "MUSE_WAKE_SENSITIVITY", "MUSE_VM_ID"):
            os.environ.setdefault(key, value)
    os.environ.setdefault("MUSEGADGET_STATE_DIR", str(STATE_DIR))
    # The SDK refuses a trust store holding two compromised root certificates, which
    # macOS's /etc/ssl/cert.pem still does; certifi's bundle has dropped them.
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    if WAKEWORD_LIBRARY.exists():
        os.environ.setdefault("MUSE_WAKEWORD_LIBRARY", str(WAKEWORD_LIBRARY))


def adb(*args, check=True):
    tool = os.environ.get("ADB") or shutil.which("adb")
    tool = tool or str(Path("~/Library/Android/sdk/platform-tools/adb").expanduser())
    return subprocess.run([tool, *args], check=check, capture_output=True, text=True)


# ---------- Pairing over the Portal's Bluetooth ----------


class PortalBle:
    """The SDK's Bluetooth transport, carried out by the Portal (BleRelay.kt).

    Stands in for musegadget.ble_server.BleServer: the shell app connects back to this
    Mac through `adb reverse` and exchanges one JSON object per line.
    """

    def __init__(self, local_name, on_write, on_disconnect):
        self._local_name = local_name
        self._on_write = on_write
        self._on_disconnect = on_disconnect
        # Until the phone negotiates one; the same assumption the SDK's own server makes.
        self._mtu = 163
        self._lock = threading.Lock()
        self._listener = None
        self._portal = None
        self._stopped = threading.Event()

    # -- Transport (called by the SDK's SetupController) --

    def mtu(self):
        return self._mtu

    def send_packets(self, packets):
        from musegadget.ble_framing import CHUNK_STAGGER_S

        for index, packet in enumerate(packets):
            if index:
                time.sleep(CHUNK_STAGGER_S)
            self._send({"op": "notify", "data": packet.hex()})

    def disconnect(self, delay):
        threading.Timer(delay, self._send, [{"op": "disconnect"}]).start()

    # -- Lifecycle --

    def run(self):
        """Advertise through the Portal and serve until stop(). Blocks."""
        self._listener = socket.create_server(("127.0.0.1", RELAY_PORT))
        self._listener.settimeout(20)
        try:
            adb("reverse", f"tcp:{RELAY_PORT}", f"tcp:{RELAY_PORT}")
            adb("shell", "am", "start", "-n", SHELL_ACTIVITY, "--es", "pair", f"127.0.0.1:{RELAY_PORT}")
            try:
                self._portal, _ = self._listener.accept()
            except TimeoutError:
                sys.exit("The Portal did not connect. Is the shell app installed and up to date?")
            self._send({
                "op": "start", "name": self._local_name, "service": SERVICE_UUID,
                "rx": RX_UUID, "tx": TX_UUID, "company": PAIRED_FLAG_COMPANY_ID,
            })
            for line in self._portal.makefile("r", encoding="utf-8"):
                self._handle(json.loads(line))
        except OSError:
            if not self._stopped.is_set():
                raise
        finally:
            self.stop()
            adb("reverse", "--remove", f"tcp:{RELAY_PORT}", check=False)

    def stop(self):
        self._stopped.set()
        for connection in (self._portal, self._listener):
            if connection is not None:
                # shutdown() first: close() alone does not wake a thread blocked reading.
                for end in (lambda: connection.shutdown(socket.SHUT_RDWR), connection.close):
                    try:
                        end()
                    except OSError:
                        pass

    def _send(self, message):
        if self._portal is None or self._stopped.is_set():
            return
        try:
            with self._lock:
                self._portal.sendall((json.dumps(message) + "\n").encode())
        except OSError:
            pass

    def _handle(self, event):
        kind = event.get("ev")
        if kind == "write":
            self._on_write(bytes.fromhex(event["data"]))
        elif kind == "mtu":
            self._mtu = int(event["mtu"])
        elif kind == "disconnected":
            self._mtu = 163
            self._on_disconnect()
        elif kind == "advertising":
            print(f"The Portal is advertising as {self._local_name}.", flush=True)
        elif kind == "connected":
            print("The Muse app connected.", flush=True)
        elif kind == "error":
            print(f"Portal Bluetooth: {event.get('message')}", file=sys.stderr, flush=True)
            self.stop()


def pair(args):
    configure()
    # pair() imports BleServer from this module only when it runs; give it the Portal's.
    stand_in = types.ModuleType("musegadget.ble_server")
    stand_in.BleServer = PortalBle
    sys.modules["musegadget.ble_server"] = stand_in
    from musegadget.pair import PairOutcome
    from musegadget.pair import pair as open_setup

    def announce(window):
        if not window.has_sdk_token:
            sys.exit(f"MUSE_SDK_TOKEN is not set. Add it to {ENV_FILE}.")
        print(
            f"Pairing is open for {window.timeout_s // 60} minutes. In the Muse app: Settings > "
            f"Devices > turn on Developer mode > Add Device (+) > {window.ble_name}.",
            flush=True,
        )

    result = open_setup(on_open=announce, force=args.force, timeout_s=args.minutes * 60)
    if result.outcome is PairOutcome.PAIRED:
        print("Paired.")
    elif result.outcome is PairOutcome.ALREADY_PAIRED:
        print("Already paired. Use --force to pair again.")
    elif result.outcome is PairOutcome.INVALID_SDK_TOKEN:
        sys.exit(f"SDK token problem: {result.sdk_token_problem}")
    else:
        sys.exit("Pairing closed without the Muse app finishing it.")


# ---------- Status ----------


def status(args):
    configure()
    from musegadget import config, identity, muse_api

    pairing = config.load_json(config.PAIRING_FILE)
    ident = identity.load_or_create()
    print(f"Device: {ident.node_id} (shows in the Muse app as {ident.ble_name})")
    if not pairing:
        print("Not paired. Run `muse/muse.py pair` with the Portal plugged in.")
        return
    print(f"Paired as: {pairing.get('username') or 'unknown'}")
    vms, code = muse_api.fetch_vms_with_status(
        pairing["access_token"], muse_api.api_root(pairing.get("api_url_v2", ""))
    )
    if code != 200:
        print(f"Muse did not accept the saved pairing (HTTP {code}).")
        return
    for vm in vms:
        print(f"Muse: {vm['vm_id']}")


# ---------- Voice calls ----------


def portal_key():
    """The shared key a dashboard must present before it may start calls."""
    path = STATE_DIR / KEY_FILE
    if not path.exists():
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.write_text(secrets.token_urlsafe(24))
    return path.read_text().strip()


class HeyJarvis:
    """openWakeWord's "Hey Jarvis" model, with the interface of the SDK's detector."""

    name = "Hey Jarvis"
    CHUNK_BYTES = 1280 * 2  # the 80 ms of 16 kHz sound the model takes at a time

    def __init__(self):
        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models(["hey_jarvis"])  # once; kept with the package
        self._model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        self._pending = b""

    def feed(self, pcm):
        """16 kHz 16-bit mono sound, in any size of piece. True if the phrase was in it."""
        import numpy

        self._pending += pcm
        heard = False
        while len(self._pending) >= self.CHUNK_BYTES:
            chunk, self._pending = self._pending[:self.CHUNK_BYTES], self._pending[self.CHUNK_BYTES:]
            scores = self._model.predict(numpy.frombuffer(chunk, dtype=numpy.int16))
            heard = heard or max(scores.values()) >= JARVIS_THRESHOLD
        return heard

    def reset(self):
        self._model.reset()
        self._pending = b""

    def close(self):
        pass


class AnyWakeWord:
    """Several detectors listening to the same sound; a wake from any of them counts."""

    def __init__(self, detectors):
        self._detectors = detectors
        self.heard = None  # the phrase that woke it last
        # The SDK's detector brings a fast resampler for the listener to use.
        for detector in detectors:
            if hasattr(detector, "resampler"):
                self.resampler = detector.resampler
                break

    @property
    def phrases(self):
        return [getattr(detector, "name", "Hey Muse") for detector in self._detectors]

    def feed(self, pcm):
        woke = False
        for detector, phrase in zip(self._detectors, self.phrases):
            if detector.feed(pcm):
                woke, self.heard = True, phrase
        return woke

    def reset(self):
        for detector in self._detectors:
            detector.reset()

    def close(self):
        for detector in self._detectors:
            detector.close()


class CallFailed(Exception):
    """A call could not start, with a message fit for the dashboard."""


class PortalAudio:
    """VoiceCall's microphone and speaker: the Portal, at the other end of a WebSocket."""

    def __init__(self, send):
        from musegadget.voice_call import FRAME_BYTES

        self._frame_bytes = FRAME_BYTES
        self._send = send
        # Only the newest third of a second, like the SDK's own live capture.
        self._mic = asyncio.Queue(maxsize=8)

    def feed(self, pcm):
        """Microphone audio from the Portal."""
        if log.isEnabledFor(logging.DEBUG):
            self._meter(pcm)
        for start in range(0, len(pcm) - len(pcm) % 2, self._frame_bytes):
            if self._mic.full():
                self._mic.get_nowait()
            self._mic.put_nowait(pcm[start:start + self._frame_bytes])

    async def frames(self):
        while True:
            yield await self._mic.get()

    def _meter(self, pcm):
        """Debugging: log the microphone's loudest moment in each two seconds."""
        from musegadget.voice_call import pcm_rms

        now = time.monotonic()
        peak, since = getattr(self, "_metered", (0, now))
        peak = max(peak, pcm_rms(pcm))
        if now - since >= 2:
            log.debug("microphone level %d", peak)
            peak, since = 0, now
        self._metered = (peak, since)

    def clear(self):
        """Drop what was captured before now, so a call does not open with old sound."""
        while not self._mic.empty():
            self._mic.get_nowait()

    def write(self, pcm):
        # The call paces playback itself; the Portal plays each chunk as it arrives.
        self._send(AUDIO + pcm)

    def flush(self):
        self._send(json.dumps({"type": "flush"}))

    def close(self):
        pass


class PortalVideo:
    """A video call's picture: H.264 frames, decoded and shown by the dashboard."""

    def __init__(self, outbox):
        self._outbox = outbox
        self.needs_keyframe = True  # the call asks Muse for one while this is set

    def write_unit(self, unit):
        from musegadget.voice_video import is_keyframe

        key = is_keyframe(unit)
        if self._outbox.qsize() > MAX_QUEUED_MESSAGES:
            self.needs_keyframe = True
        if self.needs_keyframe and not key:
            return
        self.needs_keyframe = False
        self._outbox.put_nowait((KEYFRAME if key else DELTA) + unit)


async def refreshed(pairing):
    """The pairing with new device tokens, or None if Muse would not issue them."""
    from musegadget import config, identity, muse_api

    tokens, _ = await asyncio.to_thread(
        muse_api.refresh_device_token,
        pairing["refresh_token"], identity.load_or_create().node_id,
        muse_api.api_root(pairing.get("api_url_v2", "")), config.sdk_token(),
    )
    if not tokens:
        return None
    pairing = pairing | {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "access_token_saved_at": int(time.time()),
    }
    config.save_json(config.PAIRING_FILE, pairing)
    log.info("device token rotated")
    return pairing


async def find_muse():
    """The saved pairing and the Muse (VM) to call."""
    from musegadget import config, muse_api

    pairing = config.load_json(config.PAIRING_FILE)
    if not pairing or not pairing.get("access_token"):
        raise CallFailed("Not paired with Muse")
    if pairing.get("access_token_saved_at", 0) < time.time() - TOKEN_REFRESH_AGE_S:
        pairing = await refreshed(pairing) or pairing

    async def lookup():
        return await asyncio.wait_for(asyncio.to_thread(
            muse_api.fetch_vms_with_status, pairing["access_token"],
            muse_api.api_root(pairing.get("api_url_v2", "")),
        ), muse_api.FETCH_TIMEOUT_S)

    vms, code = await lookup()
    if code in (401, 403):
        pairing = await refreshed(pairing)
        if pairing is None:
            raise CallFailed("Muse no longer accepts this pairing; pair again")
        vms, code = await lookup()
    if code != 200 or not vms:
        raise CallFailed("Can't reach Muse")
    wanted = os.environ.get("MUSE_VM_ID")
    matches = [vm for vm in vms if vm["vm_id"] == wanted] if wanted else vms
    if len(matches) != 1:
        raise CallFailed("Several Muses on this account; set MUSE_VM_ID")
    return pairing, matches[0]


async def voice_call(audio, tell, source=None, video=None, **options):
    """One call from start to hang-up. Returns the SDK's reason for its end.

    `source` is where the caller's audio comes from when that is not the microphone
    directly: the wake-word listener, which has the words spoken since the wake queued.
    With `video` (a PortalVideo) it is a video call, which has Muse's picture but
    reports no words or progress.
    """
    from musegadget import __version__, identity, server_hosts
    from musegadget import voice_call as vc
    from musegadget.link_client import DeviceDescription, LinkSession

    pairing, vm = await find_muse()
    ident = identity.load_or_create()
    # Voice only: no control stream and no registered commands (register_device=False).
    session = LinkSession(
        noise_host=server_hosts.noise_host(pairing.get("noise_host")),
        vm_id=vm["vm_id"], vm_auth_token=vm["vm_auth_token"], app_id="hatch-web",
        register_device=False,
        device=DeviceDescription(ident.node_id, "Portal", __version__, {}),
        run_command=lambda *unused: {"ok": False, "error": "unsupported command"},
    )

    def on_event(event):
        # Kinds only: what was said never goes in the log.
        if not isinstance(event, (vc.AudioChunk, vc.TranscriptDelta)):
            log.debug("event %s", type(event).__name__)
        if isinstance(event, vc.Working):
            tell(type="working", working=event.working)
        elif isinstance(event, vc.ResponseStarted):
            tell(type="reply")
        elif isinstance(event, vc.TranscriptDelta):
            tell(type="words", text=event.text)

    stop = asyncio.Event()
    connection = asyncio.create_task(session.run(stop))
    announce = None
    try:
        await session.wait_transport_ready(vc.TRANSPORT_READY_TIMEOUT_S)
        if video is not None:
            from musegadget.video_call import VideoCall

            call = VideoCall(session, device_id=ident.node_id, video_output=video,
                             idle_timeout=30, overall_timeout=600)
        else:
            call = vc.VoiceCall(session, device_id=ident.node_id, idle_timeout=30,
                                overall_timeout=600, on_event=on_event, **options)

            async def when_ready():
                await call.ready.wait()
                tell(type="state", state="listening")

            announce = asyncio.create_task(when_ready())
        return await call.run(source or audio.frames(), audio, realtime_input=True)
    finally:
        if announce is not None:
            announce.cancel()
        stop.set()
        try:
            await asyncio.wait_for(connection, 3)
        except BaseException:
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)


class Dashboard:
    """One dashboard's connection: its key, then calls by button or by wake word."""

    calling = False  # Muse holds one call at a time, whichever dashboard asks

    def __init__(self, websocket, key):
        self._websocket = websocket
        self._key = key
        self._outbox = asyncio.Queue()
        self._audio = PortalAudio(self._outbox.put_nowait)
        self._button = asyncio.Event()  # a button asked for a call
        self._button_video = False  # ... and it was the video button
        self._call = None
        self._detector = None

    def tell(self, **message):
        self._outbox.put_nowait(json.dumps(message))

    async def serve(self):
        websocket = self._websocket
        try:
            hello = json.loads(await asyncio.wait_for(websocket.recv(), 5))
            presented = str(hello.get("key", ""))
        except Exception:
            presented = ""
        if not hmac.compare_digest(presented.encode(), self._key.encode()):
            await websocket.close(4401, "wrong key")
            return
        if hello.get("wake"):
            self._arm()

        async def pump():
            while True:
                await websocket.send(await self._outbox.get())

        tasks = [asyncio.create_task(pump()), asyncio.create_task(self._converse())]
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    self._audio.feed(message)
                    continue
                request = json.loads(message)
                if request.get("type") == "start":
                    self._button_video = bool(request.get("video"))
                    self._button.set()
                elif request.get("type") == "stop" and self._call is not None:
                    self._call.cancel()
        except Exception as error:
            log.info("dashboard connection ended (%s)", type(error).__name__)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._detector is not None:
                self._detector.close()

    def _arm(self):
        """Load the wake-word detectors that are available."""
        import warnings

        from musegadget.wakeword import WakeWordDetector, WakeWordUnavailableError

        detectors = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # "experimental model"; the README says so
                sensitivity = os.environ.get("MUSE_WAKE_SENSITIVITY", "default")
                detectors.append(WakeWordDetector(sensitivity=sensitivity))
        except (WakeWordUnavailableError, ValueError):
            log.warning("Hey Muse unavailable: run muse/build-wakeword.sh")
        try:
            detectors.append(HeyJarvis())
        except Exception as error:  # a failed download or model load must not stop Hey Muse
            log.warning("Hey Jarvis unavailable (%s)", type(error).__name__)
        if detectors:
            self._detector = AnyWakeWord(detectors)
            log.info("listening for %s", " and ".join(self._detector.phrases))
        self.tell(type="wake", armed=bool(detectors))

    async def _converse(self):
        """Wait for the button or the wake word, hold the call, and wait again."""
        from musegadget.wakeword_input import WakeWordInput

        while True:
            listener = None
            try:
                waits = [asyncio.create_task(self._button.wait())]
                if self._detector is not None:
                    # Only the detector hears this audio; nothing leaves the Mac until it wakes.
                    listener = WakeWordInput(self._audio.frames(), self._detector, realtime=True)
                    waits.append(asyncio.create_task(listener.wait()))
                try:
                    await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for wait in waits:
                        wait.cancel()
                woke = len(waits) > 1 and waits[1].done() and not waits[1].cancelled() and waits[1].result()
                self._button.clear()
                if woke:
                    log.info("heard %s", self._detector.heard)
                    if self._detector.heard == os.environ.get("MUSE_VIDEO_PHRASE", VIDEO_PHRASE):
                        await self._hold(listener, video=True)
                    else:
                        # A false wake opens a call too: end it if no speech follows the wake word.
                        await self._hold(listener, speech_timeout=5,
                                         speech_after_bytes=listener.preroll_bytes)
                else:
                    if listener is not None:
                        await listener.aclose()
                        listener = None
                    self._audio.clear()
                    await self._hold(None, video=self._button_video)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning("wake word stopped (%s)", type(error).__name__)
                self._detector = None
                self.tell(type="wake", armed=False)
            finally:
                if listener is not None:
                    await asyncio.shield(listener.aclose())

    async def _hold(self, source, video=False, **options):
        """One call, reported to the dashboard; ends by itself or when `_call` is cancelled."""
        if Dashboard.calling:
            self.tell(type="state", state="error", message="Muse is already on a call")
            return
        Dashboard.calling = True
        self.tell(type="state", state="connecting", video=video)
        picture = PortalVideo(self._outbox) if video else None
        self._call = asyncio.create_task(
            voice_call(self._audio, self.tell, source, picture, **options))
        try:
            reason = await self._call
            log.info("call ended: %s", reason)
            self.tell(type="state", state="idle")
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the dashboard went away
            log.info("call hung up")
            self.tell(type="state", state="idle")
        except CallFailed as error:
            self.tell(type="state", state="error", message=str(error))
        except Exception as error:
            # Errors from the network libraries can echo credentials; log only the kind.
            log.warning("call failed (%s)", type(error).__name__)
            self.tell(type="state", state="error", message="The call to Muse failed")
        finally:
            self._call = None
            Dashboard.calling = False


def serve(args):
    configure()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    if os.environ.get("MUSE_DEBUG"):
        log.setLevel(logging.DEBUG)  # this script's own messages only
    # Its debug logs include the Authorization header.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    import websockets
    from musegadget import tls

    tls.client_context()  # refuse to start with an unsafe certificate store

    async def run():
        key = portal_key()
        async with websockets.serve(lambda ws: Dashboard(ws, key).serve(), "0.0.0.0", args.port,
                                    max_size=1 << 16):
            log.info("voice calls on port %d", args.port)
            await asyncio.Future()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    pair_parser = commands.add_parser("pair", help="pair with the Muse app through the Portal's Bluetooth")
    pair_parser.add_argument("--force", action="store_true", help="pair again, replacing the saved pairing")
    pair_parser.add_argument("--minutes", type=int, default=10, help="how long pairing stays open")
    pair_parser.set_defaults(run=pair)
    commands.add_parser("status", help="show the pairing").set_defaults(run=status)
    serve_parser = commands.add_parser("serve", help="carry voice calls between the dashboard and Muse")
    serve_parser.add_argument("--port", type=int, default=VOICE_PORT)
    serve_parser.set_defaults(run=serve)
    commands.add_parser("key", help="print the dashboard's key").set_defaults(run=lambda args: print(portal_key()))
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
