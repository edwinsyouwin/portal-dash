#!/bin/bash
# Build the "Hey Muse" wake-word detector for this Mac: muse/build/libmuse_wakeword.dylib,
# which muse.py loads when it is there.
#
# This is the Muse Gadget SDK's linux/wakeword/build.sh, redone for macOS (clang, Mach-O,
# BSD tools): the same sources at the same pinned SDK commit, and the same three ESP
# Component Registry archives, each checked against its SHA-256 before it is unpacked.
# Needs the Xcode command line tools, git, curl and unzip.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/build"
SDK="$OUT/sdk"
SDK_REPO="https://github.com/facebookincubator/muse-gadget-sdk"
# Keep in step with the musegadget pin at the top of muse.py.
SDK_COMMIT="a5f809f621c4cbdbd37336fe17e7cdb74cebf3fe"
REGISTRY="https://components-file.espressif.com/components"

# name|version|archive|sha256, as in the SDK's build.sh at that commit.
ARCHIVES="
pytorch/executorch|1.6.0-dev.20261112|pytorch__executorch-v1.6.0-dev.20261112.zip|7885747a7a47fe81b72f52b52884c122439a3304b117d0774dc05af3e03ba312
espressif/esp-nn|1.5.0|espressif__esp-nn-v1.5.0.zip|50bdc2b89b54c1fae5d308a5961bb7a261453bbcc4e384ff1c696dea7514e1ce
esphome/esp-micro-speech-features|1.2.3|esphome__esp-micro-speech-features-v1.2.3.zip|052a8f32e7a82f49593bfdc067638a8cee6542443726d1a7e5b01b3bd9d67eac
"

die() { printf 'wake-word build failed: %s\n' "$*" >&2; exit 1; }

mkdir -p "$OUT/archives"
if [ "$(git -C "$SDK" rev-parse HEAD 2>/dev/null)" != "$SDK_COMMIT" ]; then
  rm -rf "$SDK"
  git init -q "$SDK"
  git -C "$SDK" fetch -q --depth 1 "$SDK_REPO" "$SDK_COMMIT" || die "couldn't fetch the SDK"
  git -C "$SDK" checkout -q FETCH_HEAD
fi
WW="$SDK/esp32/components/muse_wakeword"

WORK=$(mktemp -d "$OUT/.ww-build.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
SRC="$WORK/src"
OBJ="$WORK/obj"
mkdir -p "$SRC" "$OBJ"

for entry in $ARCHIVES; do
  IFS='|' read -r name version file sha <<EOF
$entry
EOF
  archive="$OUT/archives/$file"
  if [ ! -f "$archive" ]; then
    printf 'Downloading %s %s\n' "$name" "$version"
    curl -fsSL --retry 3 -o "$archive.part" "$REGISTRY/$name/$version/$file" || die "couldn't download $name"
    mv "$archive.part" "$archive"
  fi
  actual=$(shasum -a 256 "$archive" | awk '{print $1}')
  if [ "$actual" != "$sha" ]; then
    rm -f "$archive"
    die "$file doesn't match its SHA-256 (got $actual)"
  fi
  mkdir -p "$SRC/${name#*/}"
  unzip -q -o "$archive" -d "$SRC/${name#*/}"
done

ET="$SRC/executorch"
NN="$SRC/esp-nn"
MF="$SRC/esp-micro-speech-features"

COMMON="-O2 -fPIC -DNDEBUG -w"
ETDEF="-DC10_USING_CUSTOM_GENERATED_MACROS -DET_USE_BUILTIN_FUNCTION_NAME=0 -DFLATBUFFERS_MAX_ALIGNMENT=1024 -DET_ENABLE_PROGRAM_VERIFICATION=1 -DET_LOG_ENABLED=0 -DET_MIN_LOG_LEVEL=Error"
CXXONLY="-std=c++17 -fno-exceptions -fno-rtti -fno-threadsafe-statics"
INC_ET="-I$ET/include -I$ET/priv_include"
INC_NN="-I$NN/include -I$NN/src/common"
INC_MF="-I$MF/include"

count=0
compile() { # language, source, extra flags (word-split on purpose: no path here has spaces)
  local lang=$1 src=$2
  shift 2
  count=$((count + 1))
  local obj="$OBJ/$count-$(basename "$src").o"
  # shellcheck disable=SC2086
  if [ "$lang" = c++ ]; then
    c++ $COMMON $CXXONLY "$@" -c "$src" -o "$obj"
  else
    cc $COMMON -std=c11 "$@" -c "$src" -o "$obj"
  fi
}

# The runtime's own source lists, as the ESP-IDF component builds it.
sources() { sed -n "/^set($1/,/^)/p" "$ET/et_generated.cmake" | grep -o '"src/[^"]*"' | tr -d '"'; }
[ "$(sources ET_ESPNN_SRCS | wc -l)" -eq 3 ] || die "unexpected ExecuTorch source lists"

case "$OUT" in *" "*) die "the build path must not contain spaces" ;; esac
# shellcheck disable=SC2086
{
  for f in $(sources ET_CORE_SRCS) $(sources ET_ESPNN_SRCS); do
    compile c++ "$ET/$f" -I"$WW/host" $INC_ET $INC_NN $ETDEF
  done
  compile c++ "$WW/host/et_pal.cpp" $INC_ET $ETDEF
  for f in "$NN"/src/*/*_ansi.c "$NN"/src/convolution/esp_nn_conv_opt.c \
           "$NN"/src/convolution/esp_nn_depthwise_conv_opt.c "$NN"/src/softmax/esp_nn_softmax_opt.c \
           "$NN"/src/common/esp_nn_multicore.c; do
    compile cc "$f" -I"$WW/host" $INC_NN -DCONFIG_NN_OPTIMIZED=1
  done
  for f in "$MF"/src/*.c; do
    compile cc "$f" $INC_MF -I"$MF/src"
  done
  compile cc "$WW/ww_frontend.c" -I"$WW" -I"$WW/include" $INC_MF -ffp-contract=off
  compile cc "$WW/ww_detector.c" -I"$WW" -I"$WW/include"
  compile c++ "$WW/ww_runner_espnn.cpp" -I"$WW" -I"$WW/include" $INC_ET $ETDEF
  compile cc "$SDK/linux/wakeword/ww_resample.c" -ffp-contract=off
}

# The model, embedded 16-aligned. The SDK's hey_muse_pte.S.in is written for ELF; this is
# the same thing for Mach-O, where C names get a leading underscore.
pte="$WW/models/hey_muse.pte"
cat > "$OBJ/hey_muse_pte.S" <<EOF
    .section __TEXT,__const
    .balign 16
    .globl _hey_muse_pte
    .globl __binary_hey_muse_pte_start
_hey_muse_pte:
__binary_hey_muse_pte_start:
    .incbin "$pte"
    .globl __binary_hey_muse_pte_end
__binary_hey_muse_pte_end:
    .balign 4
    .globl _hey_muse_pte_length
_hey_muse_pte_length:
    .long $(stat -f %z "$pte")
EOF
compile cc "$OBJ/hey_muse_pte.S"
printf 'Compiled %d files\n' "$count"

for symbol in ww_runner_create ww_runner_destroy ww_runner_reset ww_runner_feed \
              ww_runner_set_listening ww_runner_set_suppressed ww_runner_ok ww_runner_set_score_hook \
              muse_ww_resampler_create muse_ww_resampler_destroy muse_ww_resampler_reset \
              muse_ww_resampler_feed; do
  echo "_$symbol"
done > "$OBJ/exports.txt"
c++ -dynamiclib -o "$OUT/libmuse_wakeword.dylib.part" "$OBJ"/*.o \
  -Wl,-dead_strip -Wl,-exported_symbols_list,"$OBJ/exports.txt" -lm
mv "$OUT/libmuse_wakeword.dylib.part" "$OUT/libmuse_wakeword.dylib"
printf 'Built %s (%s bytes)\n' "$OUT/libmuse_wakeword.dylib" "$(stat -f %z "$OUT/libmuse_wakeword.dylib")"
