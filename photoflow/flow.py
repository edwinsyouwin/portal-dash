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
  flow.py serve   serve out/, the calendar (gcal.py) and the task list to the Portal over the home network,
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
# Face width as a fraction of the photo's long edge. Low enough to let in photos taken
# by someone else, from further away than a selfie.
GATHER_MIN_FACE = 0.06
GATHER_MIN_FACE_ON_DAY = 0.02  # with --day: an occasion's photos are often full-length
GATHER_MIN_FACE_TOGETHER = 0.05
GATHER_MAX_TURN = 0.6  # radians the head may be turned away from the camera
PORTRAIT_FACES_WIDE = 3  # a close-up is this many face-widths across: head and shoulders
PORTRAIT_MIN_PX = 400  # close-ups cropped smaller than this are too blurry to help
PORTRAIT_MAX_PX = 1024
SUNGLASSES, EYES_CLOSED = 2, 1  # how Photos records them (glasses type, eye state)
# Close-ups of each subject sent with every picture, unless profile.toml says otherwise.
REFERENCE_PORTRAITS = 2
# Memories: a picture is set in one of the household's own outings, shown to the model
# through this many of its photos. An outing needs a few photos to count, and is not
# used again until this many pictures later.
MEMORY_PHOTOS = 2
MEMORY_INTERESTS = 2  # how many of the profile's interests one picture is told about
MEMORY_MIN_PHOTOS = 5
MEMORY_REST = 30
MEMORY_APART_S = 120
# What Photos sees in pictures that are records, not memories.
MEMORY_NOT_SCENES = {"document", "printed page", "receipt", "handwriting", "screenshot", "carton", "qr code"}
MEMORY_SKIP = ("Home",)  # Photos' name for the everyday ones; profile.toml can add more
PORT = 8766

# Live dashboard updates: the shell app asks for /dashboard/<file> and only uses an answer
# signed with this key, which scripts/live.sh gives it over USB.
DASHBOARD = ROOT.parent / "dashboard"
DEPLOY_KEY = Path("~/.config/portal-dash/deploy_key").expanduser()
# Never served: it holds this household's settings and the key for Muse calls.
PRIVATE_FILES = {"config.js"}
# The dashboard's task list, kept outside the repository.
TASKS_FILE = Path("~/.config/portal-dash/tasks.json").expanduser()
MAX_TASKS = 100
MAX_TASK_CHARS = 120
# YouTube search for the dashboard's player, with YOUTUBE_API_KEY from .env. A search
# costs 100 of the 10,000 units Google allows a day without charge.
YOUTUBE_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_RESULTS = 6
MAX_QUERY_CHARS = 100
# What /status.json answers with, so a dashboard looking for the Mac knows it found it.
SERVICE = "portal-dash"


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


# ---------- Memories ----------


def pick_memory(db, subjects, profile, history, rng):
    """One of the household's own outings to set the picture in, or None.

    Photos groups a library into "moments", each named after where it was. One is picked
    at random, places weighted equally however often they were visited, and its
    best-rated photos go to the model as the setting. Photos showing anyone but the
    subjects are never sent, nor are selfies and screenshots.
    Returns {"title", "when", "photos": [(path, caption)]}.
    """
    keys = {s.get("photos_person_id") or s["photos_name"] for s in subjects}
    skip = set(MEMORY_SKIP) | set(profile.get("memory_skip", []))
    own_album = profile.get("photos_album")
    recent = {entry.get("memory") for entry in history[:MEMORY_REST]}

    moments = {}  # moment id -> (title, [photos])
    for photo in db.photos(movies=False):
        moment = photo.moment_info
        if not moment or not moment.title or moment.title in skip or moment.title in recent:
            continue
        if photo.screenshot or photo.hidden or photo.selfie or own_album in photo.albums:
            continue
        if MEMORY_NOT_SCENES & {label.lower() for label in photo.labels}:
            continue
        if {i.uuid if i.name.startswith("_") else i.name for i in photo.person_info} - keys:
            continue
        if local_file(photo):
            moments.setdefault(moment.pk, (moment.title, []))[1].append(photo)

    by_place = {}
    for title, photos in moments.values():
        if len(photos) >= MEMORY_MIN_PHOTOS:
            by_place.setdefault(title, []).append(photos)
    if not by_place:
        return None
    best = []
    for title in rng.sample(sorted(by_place), len(by_place)):
        photos = rng.choice(by_place[title])
        for photo in sorted(photos, key=lambda p: p.score.overall, reverse=True):
            # Bursts of the same shot say nothing more: take photos minutes apart.
            apart = all(abs((photo.date - other.date).total_seconds()) >= MEMORY_APART_S for other in best)
            if apart and long_edge(local_file(photo)) >= REFERENCE_MIN_PX:
                best.append(photo)
            if len(best) == MEMORY_PHOTOS:
                break
        if best:  # else only thumbnails of that outing are on this Mac: try another
            break
    if not best:
        return None
    when = best[0].date.strftime("%B %Y")
    caption = f"is one of their own photos from {title}, {when}"
    return {"title": title, "when": when, "photos": [(Path(local_file(p)), caption) for p in best]}


# ---------- Prompt ----------


def choose(options, recent, rng):
    """Pick an option, avoiding recently used ones until all have had a turn."""
    fresh = [o for o in options if o not in recent] or options
    return rng.choice(fresh)


def build_prompt(subjects, refs, scene, style, memory=None, interests=()):
    names = [s["name"] for s in subjects]
    everyone = ", ".join(names[:-1]) + f" and {names[-1]}"
    lines = [
        f"Create one image in this style: {style}.",
        f"It shows {everyone}: {scene}.",
    ]
    if memory:
        lines += [
            "",
            f"This is a new memory for them, one that never happened but could have. They "
            f"were at {memory['title']} in {memory['when']}, and the photos from there show "
            "the place, the season, the light and the kind of thing they did. Set the "
            "picture there and invent a fresh moment in the same spirit: a different "
            "view, a different activity, all of them together. Do not copy a photo.",
        ]
        if interests:
            lines.append(
                f"Among the things they love: {', '.join(interests)}. Use one only if it "
                "suits the place; otherwise let the place decide what they are doing."
            )
    lines += ["", "The attached photos:"]
    lines += [f"- Photo {i}: {caption}." for i, (_, caption) in enumerate(refs, start=1)]
    lines += [f"- {s['name']} is {s['description']}." for s in subjects if s.get("description")]
    lines += [
        "",
        "Keep each of them clearly recognisable from the references: faces, hair, build, "
        "and for any animal the breed, coat colour and markings. All of them must appear. "
        "Where several photos show the same one, go by what the photos agree on: face "
        "shape, eyes, nose, smile and hairline, rather than any one photo's expression, "
        "hat or glasses. "
        "Use the photos of them only for likeness, not for the setting or clothing. "
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
    memory = None
    if profile.get("memories", False) and not args.source:
        library = Path(profile.get("library", DEFAULT_LIBRARY)).expanduser()
        memory = library.exists() and pick_memory(open_library(profile), subjects, profile, history, rng)
    if memory:
        # Shown under the clock on the dashboard, and how the prompt introduces the picture.
        scene = f"a new memory from {memory['title']}"
        refs = refs + memory["photos"]
    # A couple of their interests each time, not the whole list: whatever is named in
    # every prompt turns up in every picture.
    interests = profile.get("interests", [])
    interests = rng.sample(interests, min(MEMORY_INTERESTS, len(interests)))
    prompt = build_prompt(subjects, refs, scene, style, memory, interests)
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
        "memory": memory["title"] if memory else None,
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


def in_colour(image):
    """False for a black-and-white picture, which says nothing about skin, hair or coat."""
    import Quartz

    size = 16
    context = Quartz.CGBitmapContextCreate(
        None, size, size, 8, size * 4, Quartz.CGColorSpaceCreateDeviceRGB(), Quartz.kCGImageAlphaNoneSkipLast)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, size, size), image)
    small = Quartz.CGBitmapContextCreateImage(context)
    data = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(small)))
    spread = [max(data[i:i + 3]) - min(data[i:i + 3]) for i in range(0, size * size * 4, 4)]
    return sum(spread) / len(spread) > 6


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
        Quartz.kCGImageSourceThumbnailMaxPixelSize: 8192,
    })
    if image is None:
        return 0
    width, height = Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)
    pixels = int(min(side * max(width, height), width, height))
    if pixels < PORTRAIT_MIN_PX:
        return 0
    left = min(max(centre[0] * width - pixels / 2, 0), width - pixels)
    top = min(max(centre[1] * height - pixels / 2, 0), height - pixels)
    square = Quartz.CGImageCreateWithImageInRect(image, Quartz.CGRectMake(int(left), int(top), pixels, pixels))
    if not in_colour(square):
        return 0
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


def download_originals(days, folder):
    """Fetch the full-size originals of those days' photos from iCloud into `folder`,
    each named by its id in Photos. The copies Photos keeps on the Mac are often too
    small to crop a face from. Returns {photo id: path}; empty if Photos would not do it.
    """
    for day in days:
        subprocess.run(
            [sys.executable, "-m", "osxphotos", "export", str(folder), "--from-date", day.isoformat(),
             "--to-date", (day + dt.timedelta(days=1)).isoformat(), "--only-photos", "--skip-edited",
             "--skip-live", "--skip-raw", "--download-missing", "--filename", "{uuid}", "--no-progress"],
            capture_output=True,
        )
    return {path.stem: path for path in images_in(Path(folder))}


def gather(args):
    """Fill refs/ from the Photos library: close-ups of each subject's face, cropped from
    the clearest recent photos of them, and photos of everyone together.

    A subject whose folder already has photos is left alone (they may be hand-picked)
    unless --replace is given. Look through the result and delete any that are not them.

    With --day, close-ups from just those days (a wedding, a party) are added to what
    is there: every good one, not one a day, and nothing is cleared. Those days'
    full-size originals are fetched from iCloud first, for the duration.
    """
    with tempfile.TemporaryDirectory() as tmp:
        originals = download_originals(args.day, tmp) if args.day else {}
        gather_into_refs(args, originals)


def gather_into_refs(args, originals):
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

    def presentable(photo):
        """Photos someone took with care, not selfies: recent ones first, then by how
        good Photos thinks the picture is, with a nudge for Portrait mode."""
        return (photo.date.year >= recent, photo.score.overall + (0.2 if photo.portrait else 0))

    on_days = set(args.day or [])

    def folder_for(name):
        folder = REFS / name
        if on_days:
            folder.mkdir(parents=True, exist_ok=True)
            return folder
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
            if face.size >= (GATHER_MIN_FACE_ON_DAY if on_days else GATHER_MIN_FACE)
            and abs(face.yaw or 0) <= GATHER_MAX_TURN
            and face.glasses_type != SUNGLASSES and face.eye_state != EYES_CLOSED
            and not photo.selfie and (not on_days or photo.date.date() in on_days)
        ]
        usable.sort(key=lambda pair: presentable(pair[1]), reverse=True)
        days = set()  # or photos, with --day
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
            if crop_square(originals.get(photo.uuid) or local_file(photo), dest, centre, side):
                days.add(photo.uuid if on_days else day)
        print(f"{subject['name']}: {len(days)} close-ups {'added to' if on_days else 'in'} {folder}")

    folder = None if on_days else folder_for("together")
    if folder is not None:
        usable = [
            photo for photo, shown in groups
            if not photo.selfie and min(face.size for face in shown) >= GATHER_MIN_FACE_TOGETHER
            and not any(face.glasses_type == SUNGLASSES for face in shown)
        ]
        usable.sort(key=presentable, reverse=True)
        days = set()
        for photo in usable:
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


class Tasks:
    """The household's task list: a small JSON file, changed one request at a time.

    A task is {"id": number, "text": str, "done": bool}. Ids are never reused, so a task
    can be named by its id from the dashboard or by Muse without mix-ups.
    """

    def __init__(self, path=TASKS_FILE):
        self._path = path
        self._lock = threading.Lock()

    def _load(self):
        try:
            stored = json.loads(self._path.read_text())
            return stored["tasks"], stored["next_id"]
        except (OSError, ValueError, KeyError):
            return [], 1

    def list(self):
        with self._lock:
            return self._load()[0]

    def change(self, request):
        """Apply {"action": add | update | delete | clear_done, ...} and return the list.
        Raises ValueError, with a reason fit to show, for anything else."""
        if not isinstance(request, dict):
            raise ValueError("not a task request")
        with self._lock:
            tasks, next_id = self._load()
            action = request.get("action")
            if action == "add":
                if len(tasks) >= MAX_TASKS:
                    raise ValueError("the list is full; finish or remove some tasks first")
                tasks.append({"id": next_id, "text": self._text(request), "done": False})
                next_id += 1
            elif action in ("update", "delete"):
                task = next((t for t in tasks if t["id"] == request.get("id")), None)
                if task is None:
                    raise ValueError("no task with that id")
                if action == "delete":
                    tasks.remove(task)
                else:
                    if "text" in request:
                        task["text"] = self._text(request)
                    if "done" in request:
                        task["done"] = bool(request["done"])
            elif action == "clear_done":
                tasks = [t for t in tasks if not t["done"]]
            else:
                raise ValueError("action must be add, update, delete or clear_done")
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Written whole to a second file first, so a crash cannot leave half a list.
            scratch = self._path.with_suffix(".part")
            scratch.write_text(json.dumps({"tasks": tasks, "next_id": next_id}, indent=1))
            scratch.replace(self._path)
            return tasks

    @staticmethod
    def _text(request):
        text = request.get("text")
        text = " ".join(text.split()) if isinstance(text, str) else ""
        text = "".join(c for c in text if c.isprintable())[:MAX_TASK_CHARS]
        if not text:
            raise ValueError("a task needs some words")
        return text


def youtube_search(query):
    """Videos matching `query` that may be embedded: [{"id", "title", "channel", "thumbnail"}].
    Raises ValueError with a reason fit to show."""
    import html

    import httpx

    query = " ".join(str(query).split())[:MAX_QUERY_CHARS]
    key = read_env().get("YOUTUBE_API_KEY")
    if not query:
        raise ValueError("say what to look for")
    if not key:
        raise ValueError("YouTube search needs YOUTUBE_API_KEY in .env")
    try:
        response = httpx.get(YOUTUBE_SEARCH, timeout=10, params={
            "part": "snippet", "type": "video", "videoEmbeddable": "true", "safeSearch": "moderate",
            "maxResults": YOUTUBE_RESULTS, "q": query, "key": key,
        })
    except httpx.HTTPError:
        raise ValueError("YouTube could not be reached") from None  # the error's text has the key in it
    if response.status_code != 200:
        reasons = [e.get("reason") for e in response.json().get("error", {}).get("errors", [])]
        raise ValueError("today's YouTube searches are used up" if "quotaExceeded" in reasons
                         else "YouTube refused the search")
    return [
        {
            "id": item["id"]["videoId"],
            "title": html.unescape(item["snippet"]["title"]),
            "channel": html.unescape(item["snippet"]["channelTitle"]),
            "thumbnail": item["snippet"]["thumbnails"]["medium"]["url"],
        }
        for item in response.json().get("items", [])
        if item.get("id", {}).get("videoId")  # now and then a channel comes back among the videos
    ]


def dashboard_version():
    """What a dashboard compares to know its files are out of date: the last commit that
    touched dashboard/. Work in progress is not a version until it is committed, so a
    half-made change never reaches the Portal by itself. "" outside a git checkout.
    """
    found = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", "dashboard"], cwd=ROOT.parent, capture_output=True, text=True
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def deploy_key():
    """The key that signs dashboard files for the Portal, made on first use."""
    if not DEPLOY_KEY.exists():
        DEPLOY_KEY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        DEPLOY_KEY.touch(mode=0o600)
        DEPLOY_KEY.write_text(secrets.token_hex(32))
    return DEPLOY_KEY.read_text().strip()


class Handler(SimpleHTTPRequestHandler):
    generator = None  # set by serve()
    tasks = Tasks()

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
            # With the newest picture's id, so a dashboard notices a new one within seconds.
            current = load_manifest()["current"]
            picture = current["id"] if current else None
            self.send_json(200, {"service": SERVICE, "picture": picture, **self.generator.status()})
        elif path == "/youtube/search":
            from urllib.parse import parse_qs, urlsplit

            try:
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                self.send_json(200, {"videos": youtube_search(query)})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
        elif path == "/tasks.json":
            self.send_json(200, {"tasks": self.tasks.list()})
        elif path == "/calendar.json":
            import gcal

            # Read on every request, so calendars added to .env show up without a restart.
            self.send_json(200, gcal.upcoming(gcal.feeds_from(read_env())))
        elif path == "/dashboard/version":
            self.send_json(200, {"version": dashboard_version()})
        elif path.startswith("/dashboard/"):
            self.send_dashboard_file(path.removeprefix("/dashboard/"))
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/generate":
            self.send_json(*self.generator.start())
        elif self.path == "/tasks":
            # Sent as plain text by the dashboard, which spares the browser a preflight.
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 4096)
                self.send_json(200, {"tasks": self.tasks.change(json.loads(self.rfile.read(length)))})
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
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
    gather_parser.add_argument(
        "--day", type=dt.date.fromisoformat, action="append",
        help="add close-ups from this day's photos (YYYY-MM-DD; may be repeated), keeping what is there",
    )
    gather_parser.set_defaults(handler=gather)

    serve_parser = commands.add_parser("serve", help="serve out/ to the Portal")
    serve_parser.add_argument("--port", type=int, default=PORT)
    serve_parser.set_defaults(handler=serve)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
