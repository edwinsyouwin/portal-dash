// Snapshot: take a photo with the Portal's camera and have the Mac redraw it in one of
// the picture styles, keeping whoever is in it (photoflow/flow.py snapshot). The photo
// goes to the Mac, and from there to the image provider, only when "Make the picture"
// is pressed; nothing is kept on the Portal.
import config from "./config.js";
import { makingPicture, requestPicture } from "./photo.js";

const COUNTDOWN_S = 3;
const PHOTO_WIDTH = 1536; // what the image provider is sent, at most
const STILL_WIDTH = 1280; // the most the Portal's camera gives a web page
const STILL_HEIGHT = 720;
const JPEG_QUALITY = 0.9;
const ERROR_MS = 5_000;
// With no one to press the button (Muse asked): time to take the photo and show it.
const SEND_AFTER_MS = 5_000;

const button = document.getElementById("camera-button");
const screen = document.getElementById("camera");
const view = document.getElementById("camera-view");
const still = document.getElementById("camera-still");
const count = document.getElementById("camera-count");
const hint = document.getElementById("camera-hint");
const takeButton = document.getElementById("camera-take");
const useButton = document.getElementById("camera-use");
const againButton = document.getElementById("camera-again");

let camera = null; // the open camera
let photo = null; // the photo taken, a JPEG
let timer = null;

/** The shell app has to allow the camera; a plain browser asks the person itself. */
const cameraAllowed = () => !window.PortalShell || Boolean(window.PortalShell.hasCamera?.());

function close() {
  clearInterval(timer);
  camera?.getTracks().forEach((track) => track.stop());
  camera = null;
  photo = null;
  view.srcObject = null;
  screen.hidden = true;
  button.classList.remove("on");
}

/** Shows the live view (no photo yet) or the photo just taken. */
function show(taken) {
  still.hidden = !taken;
  view.hidden = taken;
  takeButton.hidden = taken;
  useButton.hidden = againButton.hidden = !taken;
  count.textContent = "";
  hint.textContent = taken ? "" : "Tap the round button, then smile";
}

async function open() {
  if (makingPicture()) return;
  screen.hidden = false;
  button.classList.add("on");
  show(false);
  try {
    camera = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: STILL_WIDTH }, height: { ideal: STILL_HEIGHT } },
    });
    view.srcObject = camera;
  } catch (error) {
    console.warn("Camera failed to open:", error);
    hint.textContent = "The camera is not available. Is the Portal\u2019s camera switch on?";
    setTimeout(() => !camera && close(), ERROR_MS);
  }
}

/**
 * The sharpest picture the camera will give. The Portal's live view is only 640 by 480
 * whatever is asked for; its still-photo mode gives twice that.
 */
async function sharpest() {
  try {
    const still = await new ImageCapture(camera.getVideoTracks()[0]).takePhoto({
      imageWidth: STILL_WIDTH,
      imageHeight: STILL_HEIGHT,
    });
    const picture = await createImageBitmap(still);
    return { picture, width: picture.width, height: picture.height };
  } catch {
    return { picture: view, width: view.videoWidth, height: view.videoHeight };
  }
}

async function capture() {
  const { picture, width, height } = await sharpest();
  const scale = Math.min(1, PHOTO_WIDTH / width);
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  canvas.getContext("2d").drawImage(picture, 0, 0, canvas.width, canvas.height);
  canvas.toBlob(
    (taken) => {
      photo = taken;
      still.src = canvas.toDataURL("image/jpeg", 0.7);
      show(true);
    },
    "image/jpeg",
    JPEG_QUALITY,
  );
}

/** Counts down on screen, takes the photo, and with `send` passes it straight on. */
function countdown(send = false) {
  if (!camera || !view.videoWidth) return;
  takeButton.hidden = true;
  hint.textContent = "";
  let left = COUNTDOWN_S;
  count.textContent = left;
  clearInterval(timer);
  timer = setInterval(() => {
    left -= 1;
    count.textContent = left || "";
    if (left) return;
    clearInterval(timer);
    hint.textContent = "Hold still…"; // the camera takes a couple of seconds over it
    capture();
    if (send) setTimeout(use, SEND_AFTER_MS);
  }, 1_000);
}

function use() {
  if (!photo) return;
  // Without a type the browser sends it as it is, with no questions asked of the Mac first.
  const body = new Blob([photo]);
  close();
  requestPicture({ method: "POST", body }, "/snapshot");
}

/** For Muse: opens the camera, counts down and sends the photo without further taps. */
export async function takeSnapshot() {
  if (!config.snapshot || !cameraAllowed() || !screen.hidden) return;
  await open();
  if (!camera) return;
  view.addEventListener("loadeddata", () => countdown(true), { once: true });
}

export function startSnapshot() {
  if (!config.server || !config.photos || !config.snapshot || !cameraAllowed()) return;
  button.hidden = false;
  button.addEventListener("click", (event) => {
    // Keep the tap from also toggling the picture-only view.
    event.stopPropagation();
    if (screen.hidden) open();
    else close();
  });
  screen.addEventListener("click", (event) => event.stopPropagation());
  takeButton.addEventListener("click", () => countdown());
  useButton.addEventListener("click", use);
  againButton.addEventListener("click", () => show(false));
  document.getElementById("camera-close").addEventListener("click", close);
}
