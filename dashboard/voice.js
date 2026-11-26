// Talk to Muse: a voice or video call through the Mac (muse/muse.py serve), started with
// the mic or camera button or, when config.muse.wakeWord is on, by saying "Hey Muse"
// (voice) or "Hey Jarvis" (video). The Portal sends its microphone, plays Muse's voice
// and shows Muse's picture; the Mac holds the call and listens for the wake words.
import config from "./config.js";

const SAMPLE_RATE = 24_000;
// Start each run of speech this far ahead, to ride out uneven arrival over Wi-Fi.
const PLAY_AHEAD_S = 0.08;
// Keep the microphone quiet this long after Muse stops, so it does not hear itself.
const ECHO_TAIL_S = 0.35;
// The Portal's microphone is quiet from across a room; too quiet and Muse does not
// notice anyone speaking.
const MIC_GAIN = 2;
const ERROR_MS = 6_000;
// Muse hangs up by itself when told goodbye (or "stop listening").
const LISTENING = "Listening… say “goodbye” to finish";
const RECONNECT_MS = 15_000;
const SILENCE = new ArrayBuffer(960 * 2);

const actions = document.getElementById("generate");
const button = document.getElementById("voice-button");
const videoButton = document.getElementById("video-button");
const screen = document.getElementById("call-video");
const canvas = document.getElementById("call-canvas");
const panel = document.getElementById("voice");
const text = document.getElementById("voice-text");

// With the wake word on, the line to the Mac and the microphone stay open all the time.
const always = Boolean(config.muse?.wakeWord);

let socket = null;
let context = null;
let microphone = null;
let playhead = 0;
let inCall = false;
let words = "";
let hideTimer = null;
let listenTimer = null;
let retryTimer = null;
let decoder = null;
let frameTime = 0;
const playing = new Set();

// ---------- What the caller sees ----------

function show(message, state) {
  clearTimeout(hideTimer);
  text.textContent = message;
  panel.dataset.state = state;
  panel.hidden = false;
  button.classList.toggle("on", state !== "error");
}

function hide() {
  panel.hidden = true;
  button.classList.remove("on");
}

function fail(message) {
  show(message, "error");
  hideTimer = setTimeout(hide, ERROR_MS);
}

// ---------- Muse's voice ----------

const speaking = () => context && context.currentTime < playhead + ECHO_TAIL_S;

function play(buffer) {
  const samples = new Int16Array(buffer);
  if (!samples.length || !context) return;
  const audio = context.createBuffer(1, samples.length, SAMPLE_RATE);
  const channel = audio.getChannelData(0);
  for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 0x8000;
  const source = context.createBufferSource();
  source.buffer = audio;
  source.connect(context.destination);
  const at = Math.max(playhead, context.currentTime + PLAY_AHEAD_S);
  source.start(at);
  playhead = at + audio.duration;
  playing.add(source);
  source.onended = () => playing.delete(source);
  // Once Muse has finished talking, it is the caller's turn again.
  clearTimeout(listenTimer);
  listenTimer = setTimeout(
    () => inCall && show(LISTENING, "listening"),
    (playhead - context.currentTime + ECHO_TAIL_S) * 1000,
  );
}

function flush() {
  for (const source of playing) source.stop();
  playing.clear();
  playhead = 0;
}

/** A short rising tone: the wake word was heard. */
function chime() {
  const tone = new OscillatorNode(context, { frequency: 660 });
  const level = new GainNode(context, { gain: 0.15 });
  tone.frequency.linearRampToValueAtTime(990, context.currentTime + 0.15);
  level.gain.linearRampToValueAtTime(0, context.currentTime + 0.2);
  tone.connect(level).connect(context.destination);
  tone.start();
  tone.stop(context.currentTime + 0.2);
}

// ---------- Muse's picture (video calls) ----------

const AUDIO = 0x41; // "A"; video frames start with "K" (keyframe) or "D"
const KEYFRAME = 0x4b;
const FRAME_US = 40_000; // Muse sends 25 frames a second

/** The codec name WebCodecs wants, read from the stream's parameter set (NAL type 7). */
function codecOf(unit) {
  const bytes = new Uint8Array(unit);
  for (let i = 0; i + 6 < bytes.length; i++) {
    if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 1 && (bytes[i + 3] & 0x1f) === 7) {
      const hex = (byte) => byte.toString(16).padStart(2, "0");
      return `avc1.${hex(bytes[i + 4])}${hex(bytes[i + 5])}${hex(bytes[i + 6])}`;
    }
  }
  return null;
}

function draw(frame) {
  if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
    canvas.width = frame.displayWidth;
    canvas.height = frame.displayHeight;
  }
  canvas.getContext("2d").drawImage(frame, 0, 0);
  frame.close();
  if (inCall && screen.hidden) {
    screen.hidden = false;
    hide(); // the picture says the call is up
  }
}

function showFrame(key, unit) {
  if (!inCall) return;
  if (!decoder) {
    // A decoder can only start from a keyframe.
    const codec = key && codecOf(unit);
    if (!codec) return;
    const starting = new VideoDecoder({
      output: draw,
      error: (error) => {
        console.warn("Video decoding failed:", error);
        if (decoder === starting) decoder = null; // start again at the next keyframe
      },
    });
    starting.configure({ codec, optimizeForLatency: true });
    decoder = starting;
  }
  decoder.decode(
    new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp: (frameTime += FRAME_US), data: unit }),
  );
}

function stopVideo() {
  if (decoder && decoder.state !== "closed") decoder.close();
  decoder = null;
  screen.hidden = true;
}

// ---------- The line to the Mac ----------

function onMessage({ data }) {
  if (typeof data !== "string") {
    const kind = new Uint8Array(data, 0, 1)[0];
    if (kind === AUDIO) play(data.slice(1));
    else showFrame(kind === KEYFRAME, data.slice(1));
    return;
  }
  const message = JSON.parse(data);
  if (message.type === "flush") flush();
  else if (message.type === "reply") words = "";
  else if (message.type === "words") show((words += message.text), "speaking");
  else if (message.type === "working" && message.working) show("Thinking…", "working");
  else if (message.type === "wake") button.classList.toggle("armed", message.armed);
  else if (message.type === "state") {
    if (message.state === "connecting") {
      // Not asked for by the button, so it was the wake word.
      if (!inCall) chime();
      inCall = true;
      show("Calling Muse…", "connecting");
    } else if (message.state === "listening") {
      show(LISTENING, "listening");
    } else {
      callEnded(message.state === "error" ? message.message : null);
    }
  }
}

function callEnded(error) {
  inCall = false;
  clearTimeout(listenTimer);
  stopVideo();
  if (error) fail(error);
  else hide();
  if (!always) close();
}

/** Opens the microphone, the speaker and the connection; resolves once the Mac answers. */
async function open() {
  context = new AudioContext({ sampleRate: SAMPLE_RATE });
  await context.audioWorklet.addModule("voice-worklet.js");
  microphone = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const frames = new AudioWorkletNode(context, "mic-frames", { numberOfOutputs: 0 });
  const gain = new GainNode(context, { gain: MIC_GAIN });
  context.createMediaStreamSource(microphone).connect(gain).connect(frames);
  frames.port.onmessage = ({ data }) => {
    if (socket?.readyState !== WebSocket.OPEN) return;
    // While Muse talks, send silence in place of the microphone.
    socket.send(speaking() ? SILENCE : data);
  };

  const host = new URL(config.server).hostname;
  const opened = new WebSocket(`ws://${host}:${config.muse.port}`);
  opened.binaryType = "arraybuffer";
  socket = opened;
  await new Promise((resolve, reject) => {
    opened.onopen = resolve;
    opened.onclose = () => reject(new Error("unreachable"));
  });
  opened.send(JSON.stringify({ key: config.muse.key, wake: always }));
  opened.onmessage = onMessage;
  opened.onclose = ({ code }) => {
    const wasInCall = inCall;
    inCall = false;
    stopVideo();
    close();
    if (code === 4401) fail("The Mac refused this dashboard's key");
    else if (wasInCall) fail("Lost the Mac");
    else hide();
    if (always && code !== 4401) retryTimer = setTimeout(connect, RECONNECT_MS);
  };
}

/** Releases the microphone, the speaker and the connection. */
function close() {
  clearTimeout(listenTimer);
  button.classList.remove("armed");
  const closing = socket;
  socket = null;
  if (closing) {
    closing.onclose = null;
    closing.close();
  }
  microphone?.getTracks().forEach((track) => track.stop());
  microphone = null;
  // Let what Muse already said finish before the speaker goes away.
  const finishing = context;
  context = null;
  if (finishing) {
    const left = Math.max(0, playhead - finishing.currentTime);
    setTimeout(() => finishing.close(), left * 1000 + 200);
  }
  playhead = 0;
}

/** Keeps the line open while the wake word is on; quietly tries again while the Mac is away. */
async function connect() {
  clearTimeout(retryTimer);
  try {
    await open();
  } catch (error) {
    console.warn("Voice connection failed:", error);
    close();
    retryTimer = setTimeout(connect, RECONNECT_MS);
  }
}

async function toggle(event) {
  // Keep the tap from also toggling the picture-only view.
  event.stopPropagation();
  if (inCall) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "stop" }));
    callEnded();
    return;
  }
  inCall = true;
  show("Calling Muse…", "connecting");
  try {
    if (!socket) {
      clearTimeout(retryTimer);
      await open();
    }
    socket.send(JSON.stringify({ type: "start", video: event.currentTarget === videoButton }));
  } catch (error) {
    console.warn("Voice call failed to start:", error);
    inCall = false;
    close();
    fail(error.name === "NotAllowedError" ? "The microphone is not available" : "Can't reach the Mac");
    if (always) retryTimer = setTimeout(connect, RECONNECT_MS);
  }
}

export function startVoice() {
  if (!config.server || !config.muse) return;
  actions.hidden = false;
  button.hidden = false;
  button.addEventListener("click", toggle);
  panel.addEventListener("click", toggle);
  screen.addEventListener("click", toggle);
  // Video needs a decoder this browser may not have.
  if ("VideoDecoder" in window) {
    videoButton.hidden = false;
    videoButton.addEventListener("click", toggle);
  }
  if (always) connect();
}
