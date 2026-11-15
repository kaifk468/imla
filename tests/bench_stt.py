"""Benchmark Whisper models on your own machine.

Usage:
    uv run python tests/bench_stt.py path/to/clip.wav [more.wav ...]
    uv run python tests/bench_stt.py --models base small.en distil-large-v3 clip.wav
    uv run python tests/bench_stt.py --record 8     # record 8 s from the mic

Prints, per model: load time, transcription time per clip, real-time factor
(<1.0 means faster than real time) and the transcript, with and without the
vocabulary prompt, so you can pick a default from measured numbers.

Clips should be 16-bit PCM WAV; they are resampled to 16 kHz mono.
"""

import argparse
import sys
import time
import wave
from math import gcd
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from imla.config import default_config, get_vocabulary  # noqa: E402

DEFAULT_MODELS = ["base", "small.en", "distil-large-v3"]


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if width != 2:
            raise SystemExit(f"{path}: expected 16-bit PCM WAV")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = data.astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        from scipy import signal

        g = gcd(sr, 16000)
        audio = signal.resample_poly(audio, 16000 // g, sr // g).astype(np.float32)
    return audio


def record(seconds: float) -> np.ndarray:
    import sounddevice as sd

    print(f"Recording {seconds:.0f}s — speak now…")
    audio = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="float32")
    sd.wait()
    return audio[:, 0]


def bench_groq(tr, clips, vocab) -> None:
    print(f"\n=== groq ({tr.DEFAULT_CLOUD_MODEL}) ===")
    for use_vocab in (False, True):
        for label, audio in clips:
            dur = len(audio) / 16000
            t1 = time.monotonic()
            try:
                text = tr.transcribe_groq(audio, vocab=vocab if use_vocab else None)
            except tr.CloudSTTError as e:
                if "no API key" in str(e):
                    print("No Groq API key found. Set GROQ_API_KEY in .env or the keychain.")
                    return
                print(f"vocab={'Y' if use_vocab else 'N'} {Path(label).name}: cloud error: {e}")
                continue
            el = time.monotonic() - t1
            print(
                f"vocab={'Y' if use_vocab else 'N'} {Path(label).name}: "
                f"{el:.2f}s for {dur:.1f}s audio\n    {text}"
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clips", nargs="*", help="WAV files")
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--beam", nargs="+", type=int, default=[1, 3])
    ap.add_argument("--record", type=float, default=0.0, help="record N seconds from the mic")
    ap.add_argument("--engine", choices=["local", "groq", "both"], default="local")
    args = ap.parse_args()

    clips = [(p, load_wav(p)) for p in args.clips]
    if args.record:
        clips.append(("<mic>", record(args.record)))
    if not clips:
        ap.error("give at least one WAV file or --record N")

    vocab = get_vocabulary(default_config())

    from imla import transcribe as tr

    if args.engine in ("groq", "both"):
        bench_groq(tr, clips, vocab)
    if args.engine == "groq":
        return 0

    for name in args.models:
        tr._model = None
        tr._model_name = name
        t0 = time.monotonic()
        try:
            tr.get_model()
        except Exception as e:  # noqa: BLE001
            print(f"\n[{name}] failed to load: {e}")
            continue
        print(f"\n=== {name}  (load {time.monotonic() - t0:.1f}s) ===")
        for beam in args.beam:
            tr.set_decode_options(beam_size=beam)
            for use_vocab in (False, True):
                for label, audio in clips:
                    dur = len(audio) / 16000
                    t1 = time.monotonic()
                    text = tr.transcribe(audio, vocab=vocab if use_vocab else None)
                    el = time.monotonic() - t1
                    print(
                        f"beam={beam} vocab={'Y' if use_vocab else 'N'} "
                        f"{Path(label).name}: {el:.2f}s for {dur:.1f}s audio "
                        f"(RTF {el / dur:.2f})\n    {text}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
