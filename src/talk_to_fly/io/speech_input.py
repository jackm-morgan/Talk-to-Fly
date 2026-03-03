
# src/talk_to_fly/io/speech_input.py
from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class SpeechConfig:
    model_name: str = "small.en"
    device: str = "cpu"          # "cuda" if available
    compute_type: str = "int8"   # good default for CPU
    sample_rate: int = 16000

    # Safety cap in case key-up isn't detected
    max_record_s: float = 15.0

    # Only relevant if you choose to use fixed-window recording elsewhere
    record_s: float = 5.0


class SpeechRecognizer:
    def __init__(self, cfg: SpeechConfig = SpeechConfig()):
        self.cfg = cfg
        self._model = None  # lazy-loaded

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.cfg.model_name,
                device=self.cfg.device,
                compute_type=self.cfg.compute_type,
            )

    def _record_while_space_held(self) -> np.ndarray:
        """
        Records audio only while SPACE is held down.
        Starts on key-down and stops on key-up (or max_record_s).
        """
        from pynput import keyboard
        import sounddevice as sd

        started = threading.Event()
        stopped = threading.Event()
        frames: list[np.ndarray] = []

        def on_press(key):
            if key == keyboard.Key.space and not started.is_set():
                started.set()

        def on_release(key):
            if key == keyboard.Key.space and started.is_set():
                stopped.set()
                return False  # stop listener
            return True

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()

        print("\033[1;36mHold SPACE to talk...\033[0m")
        started.wait()
        print("\033[1;36mRecording (release SPACE to stop)...\033[0m")

        t0 = time.time()

        def callback(indata, _frames, _time_info, status):
            # status can report xruns/overflows; keep quiet unless debugging
            frames.append(indata.copy())

        with sd.InputStream(
            samplerate=self.cfg.sample_rate,
            channels=1,
            dtype="float32",
            callback=callback,
        ):
            while not stopped.is_set():
                if (time.time() - t0) >= self.cfg.max_record_s:
                    stopped.set()
                    break
                sd.sleep(50)

        listener.join()

        if not frames:
            return np.zeros((0,), dtype=np.float32)

        audio = np.concatenate(frames, axis=0)[:, 0]
        return audio

    def listen_once(self) -> str:
        """
        Push-to-talk (hold SPACE): capture audio while held, then transcribe.
        """
        audio = self._record_while_space_held()
        if audio.size == 0:
            return ""

        self._ensure_model()
        segments, _ = self._model.transcribe(audio, language="en", vad_filter=False)
        return "".join(s.text for s in segments).strip()


def prompt_user_for_task(
    *,
    voice: bool = False,
    stt: Optional[SpeechRecognizer] = None,
    prompt: str = "\n\033[1;36mEnter UAV task :> \033[0m",
) -> str:
    if not voice:
        return input(prompt).strip()

    if stt is None:
        stt = SpeechRecognizer()

    text = stt.listen_once()
    print(f"\033[1;36mHeard: {text}\033[0m")
    return text
