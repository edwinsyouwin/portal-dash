#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx>=0.27", "osxphotos>=0.68", "pyobjc-framework-Vision", "pyobjc-framework-Quartz",
#   "icalendar>=6", "recurring-ical-events>=3",
# ]
# ///
"""Daily AI photo flow for the Portal dashboard.

  flow.py run     pick reference photos, generate today's picture, publish it to out/
  flow.py people  list the people named in the Photos library
  flow.py gather  collect close-up reference photos of each subject from Photos into refs/
  flow.py serve   serve out/ and the calendar (gcal.py) to the Portal over the home network,
                  and the dashboard's own files, signed, for updates without the USB cable

Who is in the pictures, what they do and in which styles is set in profile.toml.
"""

import argparse
import base64
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import random
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
MANIFEST = OUT / "manifest.json"
USAGE_LOG = ROOT / "logs" / "usage.csv"
DEFAULT_LIBRARY = "~/Pictures/Photos Library.photoslibrary"
# Hand-picked reference photos; when present they are used instead of the library.
REFS = ROOT / "refs"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp"}

HISTORY_LIMIT = 60  # pictures kept in out/
REFERENCE_MAX_PX = 1536  # long edge of the reference photos sent to the model
REFERENCE_MIN_PX = 1024  # library copies smaller than this are only thumbnails
CANDIDATE_POOL = 20  # pick at random among this many best-scored library photos
# `gather`: how many close-ups of each subject and group photos to collect, from how far
# back (people change), and how much of the photo a face must fill to be worth cropping.
GATHER_PORTRAITS = 10
GATHER_TOGETHER = 6
GATHER_YEARS = 3
GATHER_MIN_FACE = 0.1  # face width as a fraction of the photo's long edge
GATHER_MIN_FACE_TOGETHER = 0.05
GATHER_MAX_TURN = 0.6  # radians the head may be turned away from the camera
PORTRAIT_FACES_WIDE = 3  # a close-up is this many face-widths across: head and shoulders
PORTRAIT_MIN_PX = 480  # close-ups cropped smaller than this are too blurry to help
PORTRAIT_MAX_PX = 1024
SUNGLASSES, EYES_CLOSED = 2, 1  # how Photos records them (glasses type, eye state)
# Close-ups of each subject sent with every picture, unless profile.toml says otherwise.
REFERENCE_PORTRAITS = 2
PORT = 8766

# Live dashboard updates: the shell app asks for /dashboard/<file> and only uses an answer
# signed with this key, which scripts/live.sh gives it over USB.
DASHBOARD = ROOT.parent / "dashboard"
DEPLOY_KEY = Path("~/.config/portal-dash/deploy_key").expanduser()
# Never served: it holds this household's settings and the key for Muse calls.
PRIVATE_FILES = {"config.js"}


# ---------- Reference photos ----------


def load_profile():
    path = ROOT / "profile.toml"
    if not path.exists():
        sys.exit("No profile.toml yet: copy photoflow/profile.example.toml to it and edit it.")
    return tomllib.loads(path.read_text())


def open_library(profile):
    import osxphotos

    library = Path(profile.get("library", DEFAULT_LIBRARY)).expanduser()
    if not library.exists():
        sys.exit(f"Photos library not found at {library}; set library in profile.toml")
    return osxphotos.PhotosDB(dbfile=str(library))


def named_people(db):
    """{name: photo count} for everyone named in Photos, most photographed first."""
    return {name: count for name, count in db.persons_as_dict.items() if not name.startswith("_")}


def local_file(photo):
    """The best copy of a photo that is on this Mac, or None.

    With "Optimize Mac Storage" most originals live only in iCloud, but Photos keeps a
    smaller preview of each, which is plenty for a reference image.
    """
    if photo.path_edited or photo.path:
        return photo.path_edited or photo.path
    previews = [p for p in photo.path_derivatives if Path(p).suffix.lower() in IMAGE_SUFFIXES]
    return max(previews, key=lambda p: Path(p).stat().st_size, default=None)


def long_edge(path):
    out = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], capture_output=True, text=True
    ).stdout.split()
    return max(int(out[-3]), int(out[-1]))


def pick_from_library(subjects, rng, db, own_album):
    """Choose reference photos from the Photos library.

    Prefers one photo with everyone in it; otherwise takes one photo per subject.
    Returns [(path, caption)], where the caption tells the model who is shown.
    """
    known = named_people(db)
    missing = [
        s["photos_name"]
        for s in subjects
        if not s.get("photos_person_id") and s["photos_name"] not in known
    ]
    if missing:
        sys.exit(
            f"Nobody in Photos is named {', '.join(map(repr, missing))}. "
            "Run `flow.py people` to list the names, then fix photos_name in profile.toml."
        )

    # A subject is found by name, or by the id of their face group if Photos has grouped
    # their face but nobody has named it (photos_person_id in profile.toml).
    keys = [s.get("photos_person_id") or s["photos_name"] for s in subjects]
    wanted = set(keys)

    def people_in(photo):
        return {i.uuid if i.name.startswith("_") else i.name for i in photo.person_info}

    photos = []  # (photo, who is in it), for photos showing at least one subject
    for p in db.photos(movies=False):
        present = people_in(p)
        if not (present & wanted) or p.screenshot or p.hidden or not local_file(p):
            continue
        # Never feed the generated pictures saved back into Photos in as references.
        if own_album in p.albums:
            continue
        photos.append((p, present))

    def pick(candidates):
        """A random one of the best-scored candidates that are sharp enough to use."""
        pool = []
        for photo in sorted(candidates, key=lambda p: p.score.overall, reverse=True):
            if long_edge(local_file(photo)) >= REFERENCE_MIN_PX:
                pool.append(photo)
            if len(pool) == CANDIDATE_POOL:
                break
        return rng.choice(pool) if pool else None

    everyone = ", ".join(s["name"] for s in subjects)
    photo = pick([p for p, present in photos if wanted <= present])
    if photo:
        return [(Path(local_file(photo)), f"shows {everyone} together")]

    solo = {}
    for key in keys:
        own = [(p, present) for p, present in photos if key in present]
        # Fewer other faces makes it clearer who the reference is.
        fewest = min((len(present) for _, present in own), default=0)
        solo[key] = pick([p for p, present in own if len(present) == fewest]) or pick([p for p, _ in own])
    lacking = [s["name"] for s, key in zip(subjects, keys) if not solo[key]]
    if not lacking:
        return [(Path(local_file(solo[key])), f"shows {s['name']}") for s, key in zip(subjects, keys)]

    sys.exit(
        f"No usable photos of {', '.join(map(repr, lacking))}: Photos has none of at least "
        f"{REFERENCE_MIN_PX}px stored on this Mac. Label more photos of them, open a few in "
        f"Photos to download them, or put photos in {REFS}."
    )


def images_in(folder):
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def pick_from_folder(subjects, rng, portraits):
    """Choose reference photos from refs/: a group photo from refs/together/ if there
    are any, and up to `portraits` close-ups from each subject's own folder
    (refs/<name>/, lower case). Several views of a face give a much better likeness
    than one. Returns None when refs/ holds no usable photos, so the library is used.
    """
    together = images_in(REFS / "together")
    per_subject = [images_in(REFS / s["name"].lower()) for s in subjects]
    if not together and not any(per_subject):
        return None
    empty = [s["name"].lower() for s, found in zip(subjects, per_subject) if not found]
    if empty and not together:
        sys.exit(f"Add photos to {', '.join(str(REFS / name) for name in empty)} (or to {REFS / 'together'}).")

    refs = []
    if together:
        everyone = ", ".join(s["name"] for s in subjects)
        refs.append((rng.choice(together), f"shows {everyone} together"))
    for subject, found in zip(subjects, per_subject):
        chosen = rng.sample(found, min(portraits, len(found)))
        refs += [(path, f"shows {subject['name']} alone, close up") for path in chosen]
    return refs


def refs_from_files(paths, subjects):
    """Reference photos given on the command line instead of picked from the library."""
    if len(paths) == 1:
        everyone = ", ".join(s["name"] for s in subjects)
        return [(paths[0], f"shows {everyone} together")]
    if len(paths) == len(subjects):
        return [(path, f"shows {s['name']}") for path, s in zip(paths, subjects)]
    return [(path, "shows some of them") for path in paths]


def prepare_reference(source, dest):
    """Convert a library photo (often HEIC) to a reasonably sized JPEG."""
    subprocess.run(
        ["sips", "-s", "format", "jpeg", "-Z", str(REFERENCE_MAX_PX), str(source), "--out", str(dest)],
        check=True,
        capture_output=True,
    )
    return dest


# ---------- Prompt ----------


def choose(options, recent, rng):
    """Pick an option, avoiding recently used ones until all have had a turn."""
    fresh = [o for o in options if o not in recent] or options
    return rng.choice(fresh)


def build_prompt(subjects, refs, scene, style):
    names = [s["name"] for s in subjects]
    everyone = ", ".join(names[:-1]) + f" and {names[-1]}"
    lines = [
        f"Create one image in this style: {style}.",
        f"It shows {everyone}: {scene}.",
        "",
        "The attached reference photos show who they are:",
    ]
    lines += [f"- Photo {i}: {caption}." for i, (_, caption) in enumerate(refs, start=1)]
    lines += [f"- {s['name']} is {s['description']}." for s in subjects if s.get("description")]
    lines += [
        "",
        "Keep each of them clearly recognisable from the references: faces, hair, build, "
        "and for any animal the breed, coat colour and markings. All of them must appear. "
        "Where several photos show the same one, go by what the photos agree on: face "
        "shape, eyes, nose, smile and hairline, rather than any one photo's expression, "
        "hat or glasses. "
        "Use the references only for likeness, not for the setting or clothing. "
        "Landscape 3:2 composition. No text, captions or watermarks in the image.",
    ]
    return "\n".join(lines)


# ---------- Image providers ----------
# A provider takes (prompt, reference JPEG paths, its settings table) and returns
# (image bytes, usage), where usage has input_tokens, output_tokens and cost_usd.


ENV_FILES = [ROOT / ".env", ROOT.parent / ".env"]  # later files win


def read_env():
    """Settings from the .env files (KEY=value lines), overridden by the real environment."""
    values = {}
    for env_file in ENV_FILES:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and not key.lstrip().startswith("#"):
                values[key.strip().removeprefix("export ")] = value.strip().strip("\"'")
    return values | dict(os.environ)


def api_key(name):
    value = read_env().get(name)
    if not value:
        sys.exit(f"{name} is not set. Add it to {ENV_FILES[-1]} as {name}=...")
    return value


def generate_gemini(prompt, images, settings):
    import httpx

    body = {
        "model": settings.get("model", "gemini-nano-banana-2.1"),
        "input": [{"type": "text", "text": prompt}]
        + [
            {
                "type": "image",
                "mime_type": "image/jpeg",
                "data": base64.b64encode(path.read_bytes()).decode(),
            }
            for path in images
        ],
        "response_format": {
            "type": "image",
            "aspect_ratio": "3:2",
            "image_size": settings.get("image_size", "2K"),
        },
    }
    response = httpx.post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={"x-goog-api-key": api_key("GEMINI_API_KEY"), "Api-Revision": "2026-05-20"},
        json=body,
        timeout=300,
    )
    if response.status_code != 200:
        sys.exit(f"Gemini error {response.status_code}: {response.text[:800]}")

    result = response.json()
    blocks = [
        block
        for step in result.get("steps", [])
        if step.get("type") == "model_output"
        for block in step.get("content", [])
    ]
    pictures = [b["data"] for b in blocks if b.get("type") == "image"]
    if not pictures:
        said = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        sys.exit(f"Gemini returned no image. It said: {said[:800] or '(nothing)'}")
    # Token counts are kept as Google reports them; no cost is worked out for Gemini yet.
    usage = {"input_tokens": None, "output_tokens": None, "cost_usd": None, "reported": result.get("usage")}
    return base64.b64decode(pictures[-1]), usage


def generate_openai(prompt, images, settings):
    import httpx

    response = httpx.post(
        "https://api.openai.com/v1/images/edits",
        headers={"Authorization": f"Bearer {api_key('OPENAI_API_KEY')}"},
        data={
            "model": settings.get("model", "gpt-image-2.6-sunburst"),
            "prompt": prompt,
            # 3:2, the Portal+ screen's shape.
            "size": settings.get("size", "1536x1024"),
            "quality": settings.get("quality", "medium"),
            "output_format": "jpeg",
        },
        files=[("image[]", (path.name, path.read_bytes(), "image/jpeg")) for path in images],
        timeout=600,
    )
    if response.status_code != 200:
        sys.exit(f"OpenAI error {response.status_code}: {response.text[:800]}")
    result = response.json()

    # Billed per token; the prompt's few text tokens are priced as image input here,
    # which overstates the cost by a fraction of a cent at most.
    tokens_in = result.get("usage", {}).get("input_tokens", 0)
    tokens_out = result.get("usage", {}).get("output_tokens", 0)
    cost = (
        tokens_in * settings.get("price_input", 8.0) + tokens_out * settings.get("price_output", 30.0)
    ) / 1_000_000
    usage = {"input_tokens": tokens_in, "output_tokens": tokens_out, "cost_usd": round(cost, 4)}
    return base64.b64decode(result["data"][0]["b64_json"]), usage


PROVIDERS = {
    "gemini": generate_gemini,
    "openai": generate_openai,
}


# ---------- Publishing ----------


def find_subjects(image_path):
    """Where the faces and animals are in a picture, so the dashboard can keep clear of them.

    Returns boxes as [x, y, width, height] in fractions of the picture, origin top-left.
    Uses Apple's Vision framework, which also copes with painted and cartoon styles;
    falls back to people, then to whatever draws the eye, when it finds no faces or animals.
    """
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(image_path.resolve()))

    def detect(request_class, salient=False):
        request = request_class.alloc().init()
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        handler.performRequests_error_([request], None)
        found = request.results() or []
        if salient:
            found = [obj for result in found for obj in result.salientObjects() or []]
        # Vision measures from the bottom-left corner.
        return [
            [
                round(box.origin.x, 3),
                round(1 - box.origin.y - box.size.height, 3),
                round(box.size.width, 3),
                round(box.size.height, 3),
            ]
            for box in (obj.boundingBox() for obj in found)
        ]

    return (
        detect(Vision.VNDetectFaceRectanglesRequest) + detect(Vision.VNRecognizeAnimalsRequest)
        or detect(Vision.VNDetectHumanRectanglesRequest)
        or detect(Vision.VNGenerateAttentionBasedSaliencyImageRequest, salient=True)
    )


def load_manifest():
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return {"current": None, "history": []}


def publish(image_bytes, entry, history):
    """Write the picture and manifest that the dashboard polls."""
    OUT.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".img") as raw:
        raw.write(image_bytes)
        raw.flush()
        # Providers return PNG or JPEG; normalise to JPEG to keep downloads small.
        subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "90", raw.name, "--out", str(OUT / entry["file"])],
            check=True,
            capture_output=True,
        )

    entry["subjects"] = find_subjects(OUT / entry["file"])

    entries = [entry] + history
    for old in entries[HISTORY_LIMIT:]:
        (OUT / old["file"]).unlink(missing_ok=True)
    entries = entries[:HISTORY_LIMIT]
    MANIFEST.write_text(json.dumps({"current": entry, "history": entries}, indent=1))


def record_usage(entry, model):
    """Append one line per generated picture to logs/usage.csv.

    Unlike the manifest, this is never pruned, so it is the full spending record.
    """
    USAGE_LOG.parent.mkdir(exist_ok=True)
    is_new = not USAGE_LOG.exists()
    with USAGE_LOG.open("a", newline="") as log:
        writer = csv.writer(log)
        if is_new:
            writer.writerow(["generated", "picture", "provider", "model", "input_tokens", "output_tokens", "cost_usd"])
        usage = entry["usage"]
        writer.writerow(
            [
                dt.datetime.now().isoformat(timespec="seconds"),
                entry["file"],
                entry["provider"],
                model,
                usage["input_tokens"],
                usage["output_tokens"],
                usage["cost_usd"],
            ]
        )


ADD_TO_PHOTOS = """
on run {imagePath, albumName, itemTitle, itemDescription}
    set imageFile to POSIX file imagePath
    -- Photos can take a while to answer when it has to launch first.
    with timeout of 300 seconds
        tell application "Photos"
            if not (exists album albumName) then make new album named albumName
            set added to import {imageFile} into album albumName skip check duplicates true
            repeat with addedItem in added
                set name of addedItem to itemTitle
                set description of addedItem to itemDescription
            end repeat
        end tell
    end timeout
end run
"""


def sync_to_photos(album):
    """Copy every picture not yet in the Photos library into `album` there.

    Works through the manifest, so pictures that could not be added earlier (Photos
    not permitted yet, say) are picked up on a later run.
    """
    manifest = load_manifest()
    for entry in reversed(manifest["history"]):  # oldest first, so the album reads in order
        if entry.get("in_photos"):
            continue
        result = subprocess.run(
            [
                "osascript",
                "-e",
                ADD_TO_PHOTOS,
                str(OUT / entry["file"]),
                album,
                entry["scene"][:1].upper() + entry["scene"][1:],
                f"{entry['style']}. Generated {entry['date']} by portal-dash.",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Could not add {entry['file']} to Photos: {result.stderr.strip()}", file=sys.stderr)
            break
        entry["in_photos"] = True
        print(f"Added {entry['file']} to the {album!r} album in Photos")
    MANIFEST.write_text(json.dumps(manifest, indent=1))


# ---------- Commands ----------


def run(args):
    profile = load_profile()
    subjects = profile["subjects"]
    day = (args.date or dt.date.today()).isoformat()
    rng = random.Random()

    history = load_manifest()["history"]
    scenes, styles = profile["scenes"], profile["styles"]
    scene = choose(scenes, [e["scene"] for e in history[: len(scenes) - 1]], rng)
    style = choose(styles, [e["style"] for e in history[: len(styles) - 1]], rng)

    if args.source:
        refs = refs_from_files(args.source, subjects)
    else:
        portraits = profile.get("reference_portraits", REFERENCE_PORTRAITS)
        refs = pick_from_folder(subjects, rng, portraits) or pick_from_library(
            subjects, rng, open_library(profile), profile.get("photos_album")
        )
    prompt = build_prompt(subjects, refs, scene, style)
    print(f"Date:   {day}\nScene:  {scene}\nStyle:  {style}")
    for path, caption in refs:
        print(f"Photo:  {path} ({caption})")
    print(f"\n{prompt}\n")
    if args.dry_run:
        return

    provider = profile.get("provider", "gemini")
    if provider not in PROVIDERS:
        sys.exit(f"Unknown provider {provider!r}; available: {', '.join(PROVIDERS)}")
    with tempfile.TemporaryDirectory() as tmp:
        prepared = [
            prepare_reference(path, Path(tmp) / f"ref{i}.jpg") for i, (path, _) in enumerate(refs)
        ]
        image, usage = PROVIDERS[provider](prompt, prepared, profile.get(provider, {}))

    # A day can have several pictures (the dashboard's button makes extra ones);
    # the dashboard shows a new one whenever the current id changes.
    picture_id = f"{day}-{int(time.time())}"
    entry = {
        "id": picture_id,
        "date": day,
        "file": f"{picture_id}.jpg",
        "scene": scene,
        "style": style,
        "provider": provider,
        "usage": usage,
    }
    publish(image, entry, history)
    record_usage(entry, profile.get(provider, {}).get("model", ""))
    cost = "unknown" if usage["cost_usd"] is None else f"${usage['cost_usd']:.4f}"
    print(f"Tokens: {usage['input_tokens']} in, {usage['output_tokens']} out. Cost: {cost}")
    print(f"Published {OUT / entry['file']}")
    if profile.get("photos_album"):
        sync_to_photos(profile["photos_album"])


def people(args):
    profile = load_profile()
    known = named_people(open_library(profile))
    if not known:
        sys.exit("No named people in this library. Name them in Photos > People & Pets first.")
    for name, count in list(known.items())[: args.limit]:
        print(f"{count:6}  {name}")


# ---------- Gathering reference photos ----------


def crop_square(source, dest, centre, side):
    """Save the square of `source` around `centre` as a JPEG. `centre` and `side` are
    fractions of the picture's width and height, and of its long edge. Returns the
    square's size in pixels before any shrinking, or 0 if it could not be read.
    """
    import Quartz
    from Foundation import NSURL

    image_source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(source)), None)
    if image_source is None:
        return 0
    # Read through the thumbnail call because it turns the picture upright.
    image = Quartz.CGImageSourceCreateThumbnailAtIndex(image_source, 0, {
        Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
        Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
        Quartz.kCGImageSourceThumbnailMaxPixelSize: 4096,
    })
    if image is None:
        return 0
    width, height = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    pixels = int(min(side * max(width, height), width, height))
    left = min(max(centre[0] * width - pixels / 2, 0), width - pixels)
    top = min(max(centre[1] * height - pixels / 2, 0), height - pixels)
    square = Quartz.CGImageCreateWithImageInRect(image, Quartz.CGRectMake(int(left), int(top), pixels, pixels))
    with tempfile.NamedTemporaryFile(suffix=".png") as raw:
        out = Quartz.CGImageDestinationCreateWithURL(NSURL.fileURLWithPath_(raw.name), "public.png", 1, None)
        Quartz.CGImageDestinationAddImage(out, square, None)
        if not Quartz.CGImageDestinationFinalize(out):
            return 0
        subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "90", "-Z", str(min(pixels, PORTRAIT_MAX_PX)),
             raw.name, "--out", str(dest)],
            check=True, capture_output=True,
        )
    return pixels


def gather(args):
    """Fill refs/ from the Photos library: close-ups of each subject's face, cropped from
    the clearest recent photos of them, and photos of everyone together.

    A subject whose folder already has photos is left alone (they may be hand-picked)
    unless --replace is given. Look through the result and delete any that are not them.
    """
    profile = load_profile()
    subjects = profile["subjects"]
    db = open_library(profile)
    own_album = profile.get("photos_album")
    keys = [s.get("photos_person_id") or s["photos_name"] for s in subjects]
    recent = dt.datetime.now().year - GATHER_YEARS

    faces = {key: [] for key in keys}  # (face, photo) for each subject
    groups = []  # (photo, the subjects' faces in it)
    for photo in db.photos(movies=False):
        if photo.screenshot or photo.hidden or own_album in photo.albums or not local_file(photo):
            continue
        shown = {}
        for face in photo.face_info:
            person = face.person_info
            key = person and (person.uuid if person.name.startswith("_") else person.name)
            if key in faces:
                shown[key] = face
                faces[key].append((face, photo))
        if len(shown) == len(keys):
            groups.append((photo, list(shown.values())))

    def newest_and_clearest(photo, quality):
        return (photo.date.year >= recent, quality)

    def folder_for(name):
        folder = REFS / name
        if images_in(folder) and not args.replace:
            print(f"{name}: has photos already, left alone (--replace to start again)")
            return None
        folder.mkdir(parents=True, exist_ok=True)
        for old in images_in(folder):
            old.unlink()
        return folder

    for subject, key in zip(subjects, keys):
        folder = folder_for(subject["name"].lower())
        if folder is None:
            continue
        usable = [
            (face, photo) for face, photo in faces[key]
            if face.size >= GATHER_MIN_FACE and abs(face.yaw or 0) <= GATHER_MAX_TURN
            and face.glasses_type != SUNGLASSES and face.eye_state != EYES_CLOSED
        ]
        usable.sort(key=lambda pair: newest_and_clearest(pair[1], pair[0].quality or 0), reverse=True)
        days = set()
        for face, photo in usable:
            if len(days) == GATHER_PORTRAITS:
                break
            day = photo.date.date()
            if day in days:  # one a day, for different light, hair and clothes
                continue
            # Photos measures from the bottom-left of the upright picture. The square sits
            # a little below the face's centre, to take in the shoulders and all the hair.
            side = face.size * PORTRAIT_FACES_WIDE
            longest = max(photo.width, photo.height)
            centre = (face.center_x, 1 - face.center_y + 0.15 * side * longest / photo.height)
            # Skip a face cut off by the photo's edge, and any square that would take in
            # part of someone else's face and muddle who the close-up is of.
            reach = face.size * longest / 2
            if not (reach <= face.center_x * photo.width <= photo.width - reach
                    and reach <= face.center_y * photo.height <= photo.height - reach):
                continue
            if any(
                abs(other.center_x - centre[0]) * photo.width < (side + other.size) * longest / 2
                and abs(1 - other.center_y - centre[1]) * photo.height < (side + other.size) * longest / 2
                for other in photo.face_info if other.uuid != face.uuid
            ):
                continue
            dest = folder / f"{day}-{photo.uuid[:8]}.jpg"
            if crop_square(local_file(photo), dest, centre, side) >= PORTRAIT_MIN_PX:
                days.add(day)
            else:
                dest.unlink(missing_ok=True)
        print(f"{subject['name']}: {len(days)} close-ups in {folder}")

    folder = folder_for("together")
    if folder is not None:
        usable = [
            (photo, min(face.size for face in shown), min(face.quality or 0 for face in shown))
            for photo, shown in groups
        ]
        usable = [entry for entry in usable if entry[1] >= GATHER_MIN_FACE_TOGETHER]
        usable.sort(key=lambda entry: newest_and_clearest(entry[0], entry[1] * (1 + entry[2])), reverse=True)
        days = set()
        for photo, _, _ in usable:
            if len(days) == GATHER_TOGETHER:
                break
            day = photo.date.date()
            if day in days or long_edge(local_file(photo)) < REFERENCE_MIN_PX:
                continue
            prepare_reference(local_file(photo), folder / f"{day}-{photo.uuid[:8]}.jpg")
            days.add(day)
        print(f"Together: {len(days)} photos in {folder}")
    print(f"Look through {REFS} and delete any photo that does not show who it should.")


class Generator:
    """Makes extra pictures on request from the dashboard's button, one at a time."""

    def __init__(self, daily_limit):
        self.daily_limit = daily_limit
        self.lock = threading.Lock()
        self.state = "idle"  # idle, running or error
        self.message = ""
        self.day = None
        self.made_today = 0

    def status(self):
        return {"state": self.state, "message": self.message}

    def start(self):
        """Begin a generation unless one is running or today's allowance is used up."""
        with self.lock:
            if self.state == "running":
                return 202, self.status()
            today = dt.date.today()
            if today != self.day:
                self.day, self.made_today = today, 0
            if self.made_today >= self.daily_limit:
                return 429, {
                    "state": "error",
                    "message": f"Limit of {self.daily_limit} extra pictures a day reached",
                }
            self.made_today += 1
            self.state, self.message = "running", ""
        threading.Thread(target=self.work, daemon=True).start()
        return 202, self.status()

    def work(self):
        result = subprocess.run([sys.executable, __file__, "run"], capture_output=True, text=True)
        lines = (result.stderr or result.stdout).strip().splitlines()
        with self.lock:
            if result.returncode == 0:
                self.state, self.message = "idle", ""
            else:
                self.state, self.message = "error", (lines[-1] if lines else "Generation failed")[:300]


def deploy_key():
    """The key that signs dashboard files for the Portal, made on first use."""
    if not DEPLOY_KEY.exists():
        DEPLOY_KEY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        DEPLOY_KEY.touch(mode=0o600)
        DEPLOY_KEY.write_text(secrets.token_hex(32))
    return DEPLOY_KEY.read_text().strip()


class Handler(SimpleHTTPRequestHandler):
    generator = None  # set by serve()

    def send_dashboard_file(self, name):
        """One of the dashboard's files, with a signature over its name and contents."""
        path = DASHBOARD / name
        # Plain file names only: nothing outside dashboard/, and not the private settings.
        if "/" in name or name.startswith(".") or name in PRIVATE_FILES or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        signature = hmac.new(deploy_key().encode(), name.encode() + b"\n" + body, hashlib.sha256)
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Signature", signature.hexdigest())
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # The dashboard runs on a different origin and caches pictures itself.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/status.json":
            self.send_json(200, self.generator.status())
        elif path == "/calendar.json":
            import gcal

            # Read on every request, so calendars added to .env show up without a restart.
            self.send_json(200, gcal.upcoming(gcal.feeds_from(read_env())))
        elif path.startswith("/dashboard/"):
            self.send_dashboard_file(path.removeprefix("/dashboard/"))
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/generate":
            self.send_json(*self.generator.start())
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def serve(args):
    OUT.mkdir(exist_ok=True)
    profile = load_profile()
    Handler.generator = Generator(profile.get("button_daily_limit", 10))
    server = ThreadingHTTPServer(("0.0.0.0", args.port), partial(Handler, directory=str(OUT)))
    print(f"Serving {OUT} on port {args.port}")
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="generate and publish today's picture")
    run_parser.add_argument("--date", type=dt.date.fromisoformat, help="generate for this day (YYYY-MM-DD)")
    run_parser.add_argument(
        "--source",
        type=Path,
        nargs="+",
        help="use these reference photos instead of the Photos library: "
        "one group photo, or one per subject in profile.toml order",
    )
    run_parser.add_argument("--dry-run", action="store_true", help="show the choices and prompt, generate nothing")
    run_parser.set_defaults(handler=run)

    people_parser = commands.add_parser("people", help="list the people named in the Photos library")
    people_parser.add_argument("--limit", type=int, default=30, help="how many names to show")
    people_parser.set_defaults(handler=people)

    gather_parser = commands.add_parser("gather", help="collect reference photos from Photos into refs/")
    gather_parser.add_argument("--replace", action="store_true", help="start again where refs/ already has photos")
    gather_parser.set_defaults(handler=gather)

    serve_parser = commands.add_parser("serve", help="serve out/ to the Portal")
    serve_parser.add_argument("--port", type=int, default=PORT)
    serve_parser.set_defaults(handler=serve)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
