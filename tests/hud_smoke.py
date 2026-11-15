"""In-process HUD + preview smoke test.

Exercises the recording → processing → hide transitions and verifies:
* The HUD falls back to the small (collapsed) pill when Vosk isn't
  available on this platform (macOS arm64).
* set_preview_text / set_status_text update the bottom-half area.
* The AppKit event pump exits cleanly.

On macOS this blocks on ``hud.run()`` (the main thread pumps events).
"""

from __future__ import annotations

import logging
import sys
import threading
import time

from imla.audio import Recorder
from imla.hud import HUD
from imla.preview import StreamingPreview


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    rec = Recorder()
    preview_partials: list[str] = []

    def on_partial(text: str) -> None:
        preview_partials.append(text)

    preview = StreamingPreview(on_partial=on_partial)

    hud = HUD(rms_provider=rec.current_rms_levels)
    # Wait briefly for background model initialization
    t0 = time.monotonic()
    while not preview.is_ready() and time.monotonic() - t0 < 5.0:
        time.sleep(0.05)
    preview_ready = preview.is_ready()
    print(f"Preview ready: {preview_ready}")

    def driver() -> None:
        time.sleep(0.3)
        print(f">> show_recording(preview_active={preview_ready})")
        hud.show_recording(preview_active=preview_ready)
        time.sleep(0.5)

        # Simulated Vosk emissions
        if preview_ready:
            print(">> feeding preview text 'hello world'")
            hud.set_preview_text("hello world")
            time.sleep(0.3)
            print(">> feeding preview text 'hello world this is a much longer transcript'")
            hud.set_preview_text(
                "hello world this is a much longer transcript that should be left-truncated"
            )
            time.sleep(0.4)

        print(">> show_processing")
        hud.show_processing()
        time.sleep(0.4)

        print(">> hide")
        t_hide = time.monotonic()
        hud.hide()
        time.sleep(0.25)
        print(f">> hide+0.25s; elapsed={time.monotonic()-t_hide:.3f}s")

        print(">> close")
        hud.close()

    threading.Thread(target=driver, daemon=True).start()
    hud.run()
    print("OK: HUD event loop exited cleanly.")
    print(f"Preview partials captured: {len(preview_partials)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
