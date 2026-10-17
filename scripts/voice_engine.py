import array
import asyncio
import audioop
import base64
import hashlib
import io
import logging
import math
import os
import re
import struct
import tempfile
import threading
import time
import unicodedata
import uuid
import wave
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import discord
from discord.opus import Decoder, OpusError
from discord.ext import voice_recv
from discord.ext.voice_recv import gateway as vr_gateway
from discord.ext.voice_recv import opus as vr_opus
from discord.ext.voice_recv import reader as vr_reader
from discord.ext.voice_recv import video as vr_video
from discord.ext.voice_recv import voice_client as vr_vc
from discord.ext.voice_recv.rtp import SilencePacket
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
import speech_recognition as sr
from urllib3.util.retry import Retry

try:
    import davey
except ImportError:
    davey = None

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

try:
    from gtts import gTTS
    HAS_GTTS = True
except ImportError:
    HAS_GTTS = False


# Silenzia i warning/errori interni innocui di discord-ext-voice-recv (pacchetti RTCP/comfort noise)
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.CRITICAL)
logging.getLogger("discord.ext.voice_recv.opus").setLevel(logging.ERROR)
logging.getLogger("discord.ext.voice_recv.gateway").setLevel(logging.ERROR)


def patch_voice_recv_for_dave() -> None:
    """
    Applica la patch di compatibilità per il protocollo Discord DAVE (E2EE, Voice Gateway v8, close code 4017)
    a `discord-ext-voice-recv`.
    """
    if getattr(vr_opus.PacketDecoder, "_dave_patched", False):
        return

    def _safe_video_streams_init(self, *, data, vc):
        self.audio_ssrc = data.get("audio_ssrc", 0)
        self.video_ssrc = data.get("video_ssrc", 0)
        self.member = vc.guild.get_member(int(data["user_id"])) if "user_id" in data else None
        self.streams = self._get_streams(data.get("streams") or [])

    vr_video.VoiceVideoStreams.__init__ = _safe_video_streams_init

    _orig_hook = vr_gateway.hook

    async def _safe_dave_hook(ws, msg: Dict[str, Any]):
        if not isinstance(msg, dict):
            return
        op = msg.get("op")
        data = msg.get("d")
        if not isinstance(data, dict):
            return
        try:
            await _orig_hook(ws, msg)
        except Exception as e:
            logging.debug(f"Ignorato errore hook voice_recv su op={op}: {e}")

    vr_gateway.hook = _safe_dave_hook
    vr_vc.hook = _safe_dave_hook

    _orig_init = vr_opus.PacketDecoder.__init__
    _orig_reset = vr_opus.PacketDecoder.reset

    def _patched_init(self, router, ssrc: int):
        _orig_init(self, router, ssrc)
        self._dave_ok: Set[int] = set()
        self._dave_bad: Set[int] = set()
        self._dave_drops: int = 0
        self._opus_errors: int = 0

    def _patched_reset(self) -> None:
        _orig_reset(self)
        if hasattr(self, "_dave_ok"):
            self._dave_ok.clear()
        if hasattr(self, "_dave_bad"):
            self._dave_bad.clear()

    def _dave_decrypt_inner(self, packet: Any) -> bool:
        if packet.is_silence():
            return True

        vc = self.sink.voice_client
        state = getattr(vc, "_connection", None)
        if not getattr(state, "dave_protocol_version", 0):
            return True

        session = getattr(state, "dave_session", None)
        if session is None or not session.ready:
            return False

        user_id = self._cached_id
        if user_id is None:
            user_id = self._cached_id = vc._get_id_from_ssrc(self.ssrc)
        if user_id is None:
            return False

        try:
            packet.decrypted_data = session.decrypt(
                int(user_id), davey.MediaType.audio, bytes(packet.decrypted_data)
            )
            return True
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            if "UnencryptedWhenPassthroughDisabled" in (str(e) or repr(e)):
                return True
            self._dave_drops = getattr(self, "_dave_drops", 0) + 1
            return False

    def _dave_decrypt(self, packet: Any, *, peek: bool = False) -> bool:
        if davey is None or not packet or not packet.decrypted_data:
            return True

        seq = packet.sequence
        if not peek:
            if seq in self._dave_ok:
                self._dave_ok.discard(seq)
                return True
            if seq in self._dave_bad:
                self._dave_bad.discard(seq)
                return False

        ok = self._dave_decrypt_inner(packet)
        if peek:
            if len(self._dave_ok) + len(self._dave_bad) > 64:
                self._dave_ok.clear()
                self._dave_bad.clear()
            (self._dave_ok if ok else self._dave_bad).add(seq)
        return ok

    def _decode_safe(self, data: Optional[bytes], *, fec: bool) -> bytes:
        assert self._decoder is not None
        try:
            return self._decoder.decode(data, fec=fec)
        except OpusError:
            self._opus_errors = getattr(self, "_opus_errors", 0) + 1
        try:
            return self._decoder.decode(None, fec=False)
        except OpusError:
            return b"\x00" * Decoder.FRAME_SIZE

    def _patched_decode_packet(self, packet: Any) -> Tuple[Any, bytes]:
        assert self._decoder is not None
        if packet:
            pcm = self._decode_safe(packet.decrypted_data, fec=False)
            return packet, pcm

        next_packet = self._buffer.peek_next()
        if next_packet is not None and self._dave_decrypt(next_packet, peek=True):
            nextdata: bytes = next_packet.decrypted_data  # type: ignore
            pcm = self._decode_safe(nextdata, fec=True)
        else:
            pcm = self._decode_safe(None, fec=False)
        return packet, pcm

    def _patched_process_packet(self, packet: Any) -> Optional[vr_opus.VoiceData]:
        pcm = None
        member = self._get_cached_member()
        if member is None:
            self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)
            member = self._get_cached_member()

        self._last_seq = packet.sequence
        self._last_ts = packet.timestamp

        if not self._dave_decrypt(packet):
            return None

        if not self.sink.wants_opus():
            packet, pcm = self._decode_packet(packet)

        return vr_opus.VoiceData(packet, member, pcm=pcm)

    vr_opus.PacketDecoder.__init__ = _patched_init
    vr_opus.PacketDecoder.reset = _patched_reset
    vr_opus.PacketDecoder._dave_decrypt_inner = _dave_decrypt_inner
    vr_opus.PacketDecoder._dave_decrypt = _dave_decrypt
    vr_opus.PacketDecoder._decode_safe = _decode_safe
    vr_opus.PacketDecoder._decode_packet = _patched_decode_packet
    vr_opus.PacketDecoder._process_packet = _patched_process_packet
    vr_opus.PacketDecoder._dave_patched = True
    logging.info("Supporto protocollo vocale Discord DAVE (E2EE v8) attivato.")


patch_voice_recv_for_dave()


ITALIAN_TTS_VOICES: Dict[str, str] = {
    "diego": "it-IT-DiegoNeural",
    "elsa": "it-IT-ElsaNeural",
    "isabella": "it-IT-IsabellaNeural",
    "giuseppe": "it-IT-GiuseppeNeural",
}


class InterruptibleOpusSource(discord.AudioSource):
    """
    Sorgente audio Opus per Discord che permette all'assistente vocale di parlare (TTS)
    interrompendo/mettendo in pausa temporaneamente la musica in riproduzione,
    per poi riprendere la canzone dal punto esatto non appena la risposta vocale termina.
    """

    def __init__(self, main_source: discord.AudioSource):
        super().__init__()
        self.main_source = main_source
        self._override_source: Optional[discord.AudioSource] = None
        self._override_start_time: Optional[float] = None
        self._on_override_end: Optional[Callable[[float], None]] = None
        self._lock = threading.Lock()

    def is_opus(self) -> bool:
        return True

    def interrupt_with(
        self,
        override_source: discord.AudioSource,
        on_end: Optional[Callable[[float], None]] = None,
    ) -> None:
        with self._lock:
            if self._override_source is not None:
                try:
                    self._override_source.cleanup()
                except Exception:
                    pass
            self._override_source = override_source
            self._override_start_time = time.time()
            self._on_override_end = on_end

    def replace_main_source(self, new_main_source: discord.AudioSource) -> None:
        """
        Sostituisce a caldo la sorgente musicale principale (es. per cambio volume istantaneo
        o riavvio del brano da capo) senza interrompere il player di Discord.
        """
        with self._lock:
            old_main = self.main_source
            self.main_source = new_main_source
            try:
                old_main.cleanup()
            except Exception:
                pass

    def cancel_interrupt(self) -> None:
        with self._lock:
            self._finish_override_locked()

    def _finish_override_locked(self) -> None:
        if self._override_source is not None:
            try:
                self._override_source.cleanup()
            except Exception:
                pass
            self._override_source = None

        elapsed = 0.0
        if self._override_start_time is not None:
            elapsed = max(0.0, time.time() - self._override_start_time)
            self._override_start_time = None

        cb = self._on_override_end
        self._on_override_end = None
        if cb:
            try:
                cb(elapsed)
            except Exception as e:
                logging.debug(f"Errore callback fine TTS: {e}")

    def read(self) -> bytes:
        with self._lock:
            if self._override_source is not None:
                try:
                    data = self._override_source.read()
                except Exception:
                    data = b""
                if data:
                    return data
                self._finish_override_locked()

            try:
                return self.main_source.read()
            except Exception:
                return b""

    def cleanup(self) -> None:
        with self._lock:
            self._finish_override_locked()
            try:
                self.main_source.cleanup()
            except Exception:
                pass


OPUS_SILENCE_FRAME = b"\xf8\xff\xfe"


class QueuedOpusSource(discord.AudioSource):
    """
    Riproduce in sequenza più sorgenti Opus (le frasi di una risposta TTS sintetizzate in parallelo):
    la prima frase parte appena è pronta mentre le successive vengono accodate senza buchi.
    Se la frase successiva non è ancora pronta invia frame di silenzio, così la musica resta in pausa.
    """

    MAX_WAIT_FRAMES = 400  # 8 secondi di attesa massima tra una frase e l'altra

    def __init__(self, first_source: discord.AudioSource):
        super().__init__()
        self._sources: deque = deque([first_source])
        self._current: Optional[discord.AudioSource] = None
        self._input_closed = False
        self._waited_frames = 0
        self._lock = threading.Lock()

    def is_opus(self) -> bool:
        return True

    def append(self, source: discord.AudioSource) -> bool:
        with self._lock:
            if not self._input_closed:
                self._sources.append(source)
                return True
        try:
            source.cleanup()
        except Exception:
            pass
        return False

    def close_input(self) -> None:
        """Nessun'altra frase in arrivo: la sorgente termina quando l'ultima frase finisce."""
        with self._lock:
            self._input_closed = True

    def read(self) -> bytes:
        while True:
            with self._lock:
                if self._current is None:
                    if self._sources:
                        self._current = self._sources.popleft()
                    elif self._input_closed:
                        return b""
                    else:
                        self._waited_frames += 1
                        if self._waited_frames > self.MAX_WAIT_FRAMES:
                            return b""
                        return OPUS_SILENCE_FRAME
                current = self._current
                self._waited_frames = 0

            try:
                data = current.read()
            except Exception:
                data = b""
            if data:
                return data

            try:
                current.cleanup()
            except Exception:
                pass
            with self._lock:
                if self._current is current:
                    self._current = None

    def cleanup(self) -> None:
        with self._lock:
            self._input_closed = True
            pending = list(self._sources)
            self._sources.clear()
            current, self._current = self._current, None
        for src in [current, *pending]:
            if src is not None:
                try:
                    src.cleanup()
                except Exception:
                    pass


class ItalianTTSEngine:
    """
    Motore Text-to-Speech (TTS) cloud gratuito in italiano.
    """

    def __init__(self, default_voice: Optional[str] = None):
        env_voice = (os.getenv("TTS_VOICE") or "").strip().lower()
        if default_voice and default_voice.lower() in ITALIAN_TTS_VOICES:
            self.voice = ITALIAN_TTS_VOICES[default_voice.lower()]
        elif env_voice in ITALIAN_TTS_VOICES:
            self.voice = ITALIAN_TTS_VOICES[env_voice]
        elif env_voice.startswith("it-it-"):
            self.voice = os.getenv("TTS_VOICE").strip()
        else:
            self.voice = ITALIAN_TTS_VOICES["elsa"]

        self.temp_dir = Path(tempfile.gettempdir()) / "venusbot_tts"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        # Sintesi in corso per lo stesso testo: chi la richiede di nuovo aspetta quella già avviata
        self._inflight: Dict[str, "asyncio.Future[Optional[Path]]"] = {}

    def set_voice(self, voice_key: str) -> str:
        key = voice_key.strip().lower()
        if key in ITALIAN_TTS_VOICES:
            self.voice = ITALIAN_TTS_VOICES[key]
        else:
            self.voice = voice_key.strip()
        return self.voice

    @staticmethod
    def clean_text_for_speech(text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        cleaned = re.sub(r"[*_~`#>]+", "", cleaned)
        cleaned = cleaned.replace("%", " percento ")
        cleaned = cleaned.replace("&", " e ")
        cleaned = re.sub(
            r"[^\w\s.,!?;:'\"()\-àèéìòùÀÈÉÌÒÙ€$°]",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) > 650:
            cleaned = cleaned[:647].rsplit(" ", 1)[0] + "."
        return cleaned

    def split_for_speech(self, text: str) -> list[str]:
        """
        Divide la risposta in frasi per la sintesi in parallelo: la prima frase resta corta
        (così inizia a parlare prima) e le successive vengono raggruppate in blocchi più lunghi.
        """
        clean = self.clean_text_for_speech(text)
        if not clean:
            return []
        parts = [p.strip() for p in re.split(r"(?<=[.!?;:])\s+", clean) if p.strip()]
        chunks: list[str] = []
        for part in parts:
            if chunks and (
                len(chunks[-1]) < 12 or (len(chunks) > 1 and len(chunks[-1]) + len(part) < 160)
            ):
                chunks[-1] = f"{chunks[-1]} {part}"
            else:
                chunks.append(part)
        return chunks or [clean]

    def prefetch(self, text: str) -> None:
        """Avvia subito in background la sintesi della risposta (verrà riusata da speakInVC)."""
        for chunk in self.split_for_speech(text):
            asyncio.ensure_future(self.synthesize_to_file(chunk))

    async def synthesize_to_file(self, text: str) -> Optional[Path]:
        clean_text = self.clean_text_for_speech(text)
        if not clean_text:
            return None

        # Cache persistente su disco per voce + testo:
        # le frasi comuni ("Dimmi pure.", "Salto il brano.", "Musica in pausa.", ecc.)
        # vengono restituite in 0ms senza dover attendere la rete!
        cache_key = hashlib.sha1(
            f"{self.voice}:{clean_text.lower()}".encode("utf-8")
        ).hexdigest()[:16]
        cached_path = self.temp_dir / f"cached_{cache_key}.mp3"
        if cached_path.exists() and cached_path.stat().st_size > 256:
            return cached_path

        task = self._inflight.get(cache_key)
        if task is None:
            task = asyncio.ensure_future(self._synthesize_uncached(clean_text, cached_path))
            self._inflight[cache_key] = task
            task.add_done_callback(lambda _t, k=cache_key: self._inflight.pop(k, None))
        # shield: se chi aspetta viene annullato, la sintesi condivisa continua per gli altri
        return await asyncio.shield(task)

    async def _synthesize_uncached(self, clean_text: str, cached_path: Path) -> Optional[Path]:
        tmp_path = self.temp_dir / f"tmp_{uuid.uuid4().hex[:10]}.mp3"

        if HAS_EDGE_TTS:
            try:
                communicate = edge_tts.Communicate(
                    text=clean_text,
                    voice=self.voice,
                    rate="+8%",
                )
                await asyncio.wait_for(communicate.save(str(tmp_path)), timeout=8.0)
                if tmp_path.exists() and tmp_path.stat().st_size > 256:
                    try:
                        tmp_path.replace(cached_path)
                        return cached_path
                    except Exception:
                        return tmp_path
            except Exception as e:
                logging.warning(f"Edge-TTS fallito ({e}), passo al fallback gTTS...")

        if HAS_GTTS:
            try:
                loop = asyncio.get_running_loop()

                def _run_gtts():
                    tts = gTTS(text=clean_text, lang="it", slow=False)
                    tts.save(str(tmp_path))

                await asyncio.wait_for(loop.run_in_executor(None, _run_gtts), timeout=8.0)
                if tmp_path.exists() and tmp_path.stat().st_size > 256:
                    try:
                        tmp_path.replace(cached_path)
                        return cached_path
                    except Exception:
                        return tmp_path
            except Exception as e:
                logging.error(f"Anche il fallback gTTS è fallito: {e}")

        return None

    async def prewarm_common_phrases(self) -> None:
        """Pre-genera in background il suono di attivazione e le risposte vocali più frequenti (0ms)."""
        try:
            self.get_activation_chime_path()
        except Exception:
            pass
        common = (
            "Dimmi pure.",
            "Dimmi il titolo.",
            "Ciao! Dimmi pure.",
            "Sì, ti sento! Dimmi pure.",
            "Musica in pausa.",
            "Riprendo la musica.",
            "Riprendo la riproduzione.",
            "Salto il brano.",
            "Fermo la musica e svuoto la coda.",
            "Ho mescolato la coda di riproduzione.",
            "Ho svuotato la coda di riproduzione.",
            "La coda è già vuota.",
            "Rimetto la canzone dall'inizio.",
            "Ripetizione del brano attivata.",
            "Ripetizione disattivata.",
            "Di nulla!",
            "A presto!",
            "Mi dispiace, non ho capito bene.",
        )
        for phrase in common:
            try:
                await self.synthesize_to_file(phrase)
            except Exception:
                pass

    def get_activation_chime_path(self) -> Path:
        """
        Genera (e mantiene in cache) un suono di attivazione cristallino stile Google Assistant / Smart Speaker
        (doppio tono armonico ascendente D5 -> A5 da 0.25s a 48kHz stereo).
        """
        chime_path = self.temp_dir / "cached_wake_chime.wav"
        if chime_path.exists() and chime_path.stat().st_size > 1024:
            return chime_path

        sample_rate = 48000
        duration = 0.25
        total_samples = int(sample_rate * duration)
        split_sample = int(sample_rate * 0.105)

        frames = bytearray()
        for i in range(total_samples):
            t = i / sample_rate
            if i < split_sample:
                # Nota 1: Re5 (587.33 Hz) con armonica morbida
                local_t = i / sample_rate
                note_dur = split_sample / sample_rate
                env = math.sin(math.pi * (local_t / note_dur)) ** 0.7
                val = (
                    0.78 * math.sin(2.0 * math.pi * 587.33 * t)
                    + 0.22 * math.sin(2.0 * math.pi * 1174.66 * t)
                ) * env
            else:
                # Nota 2: La5 (880.00 Hz) brillante con decadimento morbido
                local_t = (i - split_sample) / sample_rate
                note_dur = (total_samples - split_sample) / sample_rate
                attack = min(1.0, local_t / 0.010)
                decay = math.exp(-local_t * 10.5)
                env = attack * decay
                val = (
                    0.80 * math.sin(2.0 * math.pi * 880.00 * t)
                    + 0.20 * math.sin(2.0 * math.pi * 1760.00 * t)
                ) * env

            sample_int = max(-32767, min(32767, int(val * 14500)))
            frames.extend(struct.pack("<hh", sample_int, sample_int))

        with wave.open(str(chime_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(bytes(frames))

        return chime_path


class ItalianDiscordAudioSource(sr.AudioSource):
    """Mantenuta per compatibilità con test unitari stereo->mono."""

    little_endian = True
    SAMPLE_RATE = 48_000
    SAMPLE_WIDTH = 2
    CHANNELS = 2
    CHUNK = 960

    def __init__(self, buffer: array.array):
        self.buffer = buffer
        self._entered = False

    @property
    def stream(self):
        return self

    def __enter__(self):
        self._entered = True
        return self

    def __exit__(self, *exc) -> None:
        self._entered = False

    def read(self, size: int) -> bytes:
        bytes_needed = size * self.CHANNELS * self.SAMPLE_WIDTH
        available = min(bytes_needed, len(self.buffer))
        aligned = available - (available % 4)
        if aligned <= 0:
            return b""
        audiochunk = self.buffer[:aligned].tobytes()
        del self.buffer[:aligned]
        return audioop.tomono(audiochunk, self.SAMPLE_WIDTH, 0.5, 0.5)

    def close(self) -> None:
        del self.buffer[:]




def _fold_text(text: str) -> str:
    """Minuscolo, senza accenti e senza punteggiatura: per confrontare trascrizioni."""
    normalized = unicodedata.normalize("NFD", text.lower())
    no_accents = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", no_accents)).strip()


# Frasi che Whisper "inventa" sul silenzio o sul rumore (sottotitoli dei dataset di addestramento)
_WHISPER_HALLUCINATIONS = {
    "sottotitoli creati dalla comunita amara org",
    "sottotitoli e revisione a cura di",
    "sottotitoli a cura di qtss",
    "grazie per aver guardato",
    "grazie per la visione",
    "thank you for watching",
    "thanks for watching",
}


class ItalianSpeechSink(voice_recv.AudioSink):
    """
    Sink vocale Discord Multi-Motore ad Alta Precisione (Utterance Collector):
    - Ricostruisce le micro-pause DTX di Discord tra una parola e l'altra (evitando che
      "Venus" e "metti" vengano incollati senza spazio audio).
    - Normalizza il volume sui PICCHI reali (Peak Normalization a ~70% full-scale)
      senza MAI clippare o distorcere la voce.
    - Trascrizione ANTICIPATA: appena l'utente fa una pausa parte già la trascrizione, così quando
      la pausa diventa "fine frase" il testo è già pronto (centinaia di ms risparmiati a ogni comando).
    - Supporta 4 motori Speech-to-Text combinati in cascata con "hedging" (se il motore principale
      è lento parte in parallelo anche il successivo, vince il primo che risponde):
      1. Deepgram Nova-3 (se DEEPGRAM_API_KEY è presente: bilingue nativo IT+EN ultra-preciso)
      2. Google Gemini Audio (se GEMINI_API_KEY è presente: comprensione audio nativa di titoli e artisti)
      3. Groq Whisper Large v3 (+ secondo passaggio EN solo per le richieste di canzoni rivolte al bot)
      4. Google Speech Recognition Multi-Alternativa (top-5 ipotesi it-IT + ipotesi en-US)
    """

    SAMPLE_RATE = 48_000
    SAMPLE_WIDTH = 2
    SHORT_SILENCE_FLUSH_SECONDS = 0.62  # Per frasi brevi ("Venus", "Venus, pausa")
    LONG_SILENCE_FLUSH_SECONDS = 0.92   # Per frasi con titolo canzone ("Venus metti ...")
    SHORT_PHRASE_SECONDS = 0.85         # Sotto questa durata di parlato la frase è considerata breve
    SPECULATIVE_SILENCE_SECONDS = 0.38  # Dopo questa pausa avvia la trascrizione anticipata
    MAX_SPECULATIONS_PER_PHRASE = 3
    MAX_PHRASE_SECONDS = 10.0           # Durata massima singola frase
    ENGINE_HEDGE_SECONDS = 1.2          # Motore lento oltre questa soglia: avvia anche il successivo
    ENGINE_TIMEOUT_SECONDS = 5.0
    _GROQ_WHISPER_MODELS = ["whisper-large-v3", "whisper-large-v3-turbo"]
    _GEMINI_STT_MODELS = [
        "gemini-flash-lite-latest",
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
        "gemini-2.0-flash",
    ]

    def __init__(
        self,
        on_transcription: Callable[[discord.Member, str], Any],
        phrase_time_limit: int = 12,
        language: str = "it-IT",
        is_command_candidate: Optional[Callable[[discord.Member, str], bool]] = None,
        get_wake_words: Optional[Callable[[], List[str]]] = None,
    ):
        self._lock = threading.Lock()
        self._user_buffers: Dict[int, Dict[str, Any]] = {}
        self._preroll_buffers: Dict[int, deque] = defaultdict(lambda: deque(maxlen=15))
        self._executor: Optional[ThreadPoolExecutor] = None
        self._io_executor: Optional[ThreadPoolExecutor] = None
        self._running = False
        super().__init__(None)
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        self.on_transcription = on_transcription
        self.phrase_time_limit = phrase_time_limit
        self.language = language
        # Permettono di fare il secondo passaggio EN (più lento) solo per le frasi rivolte al bot
        # e di suggerire allo STT la parola di attivazione configurata dagli admin.
        self.is_command_candidate = is_command_candidate
        self.get_wake_words = get_wake_words
        self.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip().strip("'\"")
        self.gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip().strip("'\"")
        self.deepgram_api_key = (os.getenv("DEEPGRAM_API_KEY") or "").strip().strip("'\"")
        self.speculative_enabled = (os.getenv("STT_SPECULATIVE") or "1").strip().lower() not in (
            "0", "false", "no", "off",
        )
        self._gemini_models: List[str] = []
        for model in [(os.getenv("GEMINI_STT_MODEL") or "").strip(), *self._GEMINI_STT_MODELS]:
            if model and model not in self._gemini_models:
                self._gemini_models.append(model)
        self._gemini_no_thinking_cfg: Set[str] = set()

        self._recognizer = sr.Recognizer()
        # Senza timeout una richiesta a Google bloccata terrebbe occupato un thread per sempre
        self._recognizer.operation_timeout = 5
        self._http = self._build_http_session()
        # Due pool separati: le frasi (che aspettano i motori) e le richieste HTTP ai motori,
        # così una frase in attesa non può mai bloccare le richieste di cui ha bisogno.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stt-worker")
        self._io_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="stt-io")
        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="italian-speech-flusher"
        )
        self._flush_thread.start()

    @staticmethod
    def _build_http_session() -> requests.Session:
        """Sessione HTTP con connessioni keep-alive: niente handshake TLS a ogni frase trascritta."""
        session = requests.Session()
        retry = Retry(total=1, connect=1, read=0, status=0, other=0, allowed_methods=None, raise_on_status=False)
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=retry)
        session.mount("https://", adapter)
        return session

    def wants_opus(self) -> bool:
        return False

    def _wake_words(self) -> List[str]:
        words: List[str] = []
        if self.get_wake_words:
            try:
                words = [w.strip() for w in self.get_wake_words() if w and w.strip()]
            except Exception:
                words = []
        return words or ["venus", "venere"]

    def _is_command_candidate(self, user: Optional[discord.Member], text: str) -> bool:
        if self.is_command_candidate is None or user is None:
            return True
        try:
            return bool(self.is_command_candidate(user, text))
        except Exception:
            return True

    def write(self, user: Optional[discord.Member], data: voice_recv.VoiceData) -> None:
        if not self._running:
            return
        if isinstance(data.packet, SilencePacket):
            return
        if user is None or getattr(user, "bot", False):
            return
        if not data.pcm or len(data.pcm) < 4:
            return

        # Allinea a 4 byte (stereo 16-bit) e converti in mono senza clipping (0.5, 0.5)
        aligned_len = len(data.pcm) - (len(data.pcm) % 4)
        if aligned_len <= 0:
            return
        mono_pcm = audioop.tomono(data.pcm[:aligned_len], self.SAMPLE_WIDTH, 0.5, 0.5)

        rms = audioop.rms(mono_pcm, self.SAMPLE_WIDTH)
        now = time.monotonic()

        with self._lock:
            entry = self._user_buffers.get(user.id)
            if entry is None:
                # Conserva sempre gli ultimi ~300ms di pre-roll per non tagliare mai l'inizio della prima parola
                self._preroll_buffers[user.id].append(mono_pcm)
                if rms < 85:
                    return
                preroll_bytes = b"".join(self._preroll_buffers[user.id])
                self._preroll_buffers[user.id].clear()
                entry = {
                    "user": user,
                    "pcm": bytearray(preroll_bytes),
                    "first_ts": now,
                    "last_ts": now,
                    "last_pkt_ts": now,
                    "spec_count": 0,
                    "spec_last_ts": None,
                    "spec_future": None,
                }
                self._user_buffers[user.id] = entry
            else:
                entry["user"] = user
                # Se Discord ha sospeso l'invio di pacchetti RTP durante una breve pausa tra due parole
                # (es. "Venus ... metti"), reinserisci i millisecondi di silenzio mancanti così le parole
                # non vengono incollate tra loro!
                pkt_gap = now - entry.get("last_pkt_ts", now)
                if 0.06 <= pkt_gap <= 0.65:
                    gap_samples = int(min(pkt_gap, 0.35) * self.SAMPLE_RATE)
                    entry["pcm"].extend(b"\x00" * (gap_samples * self.SAMPLE_WIDTH))
                entry["last_pkt_ts"] = now
                entry["pcm"].extend(mono_pcm)
                if rms >= 80:
                    entry["last_ts"] = now

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(0.03)
            now = time.monotonic()
            to_finalize = []
            to_speculate = []

            with self._lock:
                for uid, entry in list(self._user_buffers.items()):
                    silence_elapsed = now - entry["last_ts"]
                    total_elapsed = now - entry["first_ts"]
                    speech_elapsed = entry["last_ts"] - entry["first_ts"]
                    threshold = (
                        self.SHORT_SILENCE_FLUSH_SECONDS
                        if speech_elapsed < self.SHORT_PHRASE_SECONDS
                        else self.LONG_SILENCE_FLUSH_SECONDS
                    )
                    if (
                        silence_elapsed >= threshold
                        or total_elapsed >= self.MAX_PHRASE_SECONDS
                    ):
                        self._user_buffers.pop(uid, None)
                        to_finalize.append(entry)
                    elif (
                        self.speculative_enabled
                        and silence_elapsed >= self.SPECULATIVE_SILENCE_SECONDS
                        and entry["spec_last_ts"] != entry["last_ts"]
                        and entry["spec_count"] < self.MAX_SPECULATIONS_PER_PHRASE
                    ):
                        # L'utente si è fermato: trascrivi già adesso. Se riprende a parlare
                        # il risultato viene scartato, altrimenti è pronto quando la frase si chiude.
                        entry["spec_last_ts"] = entry["last_ts"]
                        entry["spec_count"] += 1
                        entry["spec_future"] = None
                        to_speculate.append((entry, bytes(entry["pcm"])))

            for entry, pcm_bytes in to_speculate:
                try:
                    entry["spec_future"] = self._executor.submit(self._transcribe, entry["user"], pcm_bytes)
                except RuntimeError:
                    pass

            for entry in to_finalize:
                self._finalize_utterance(entry)

    def _finalize_utterance(self, entry: Dict[str, Any]) -> None:
        user = entry["user"]
        spec_future: Optional[Future] = entry.get("spec_future")
        if spec_future is not None and entry.get("spec_last_ts") == entry["last_ts"]:
            # Nessuna parola nuova dopo la trascrizione anticipata: il testo è già pronto (o quasi)
            future = spec_future
        else:
            if spec_future is not None:
                spec_future.cancel()
            try:
                future = self._executor.submit(self._transcribe, user, bytes(entry["pcm"]))
            except RuntimeError:
                return
        future.add_done_callback(partial(self._deliver_transcription, user))

    def _deliver_transcription(self, user: discord.Member, future: Future) -> None:
        if future.cancelled() or not self._running:
            return
        try:
            text = future.result()
        except Exception as e:
            logging.debug(f"Errore elaborazione frase vocale: {e}")
            return
        if text and text.strip():
            self.on_transcription(user, text.strip())

    def _transcribe(self, user: discord.Member, pcm_bytes: bytes) -> Optional[str]:
        try:
            duration_sec = len(pcm_bytes) / (self.SAMPLE_RATE * self.SAMPLE_WIDTH)
            if duration_sec < 0.28:
                return None
            rms = audioop.rms(pcm_bytes, self.SAMPLE_WIDTH)
            peak = audioop.max(pcm_bytes, self.SAMPLE_WIDTH)
            if rms < 75 or peak < 350:
                return None

            # Normalizzazione sul PICCO REALE (Peak Normalization a 23000 / 32767 = ~70% full-scale):
            # amplifica i microfoni bassi ma impedisce MATEMATICAMENTE qualsiasi clipping o distorsione!
            if 400 <= peak < 19000:
                gain = min(2.4, 23000.0 / float(peak))
                try:
                    pcm_bytes = audioop.mul(pcm_bytes, self.SAMPLE_WIDTH, gain)
                except Exception:
                    pass

            # Aggiungi 200ms di silenzio pulito all'inizio e alla fine
            pad = b"\x00" * int(self.SAMPLE_RATE * self.SAMPLE_WIDTH * 0.20)
            framed_pcm = pad + pcm_bytes + pad

            audio = sr.AudioData(framed_pcm, self.SAMPLE_RATE, self.SAMPLE_WIDTH)
            return self._recognize_italian(audio, user)
        except Exception as e:
            logging.debug(f"Errore elaborazione frase vocale: {e}")
            return None

    @staticmethod
    def _looks_like_music_request(text: str) -> bool:
        t = text.lower()
        return any(
            w in t
            for w in (
                "metti",
                "mettere",
                "riproduci",
                "riprodurre",
                "suona",
                "suonare",
                "play",
                "cerca",
                "ascoltiamo",
                "ascoltare",
                "sentire",
                "canzone",
                "brano",
            )
        )

    def _run_engine_cascade(
        self, engines: List[Tuple[str, Callable[[], Optional[str]]]]
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Esegue i motori STT in ordine di priorità con "hedging": se il motore corrente fallisce si passa
        subito al successivo, se è lento (> ENGINE_HEDGE_SECONDS) il successivo parte in parallelo.
        Restituisce (nome_motore, testo) del primo motore che produce una trascrizione.
        """
        if not engines:
            return None, None
        if len(engines) == 1:
            name, fn = engines[0]
            try:
                return name, fn()
            except Exception as e:
                logging.debug(f"Motore STT {name} fallito: {e}")
                return name, None

        pending: Dict[Future, str] = {}
        next_idx = 0
        deadline = time.monotonic()

        def launch_next() -> None:
            nonlocal next_idx, deadline
            if next_idx < len(engines):
                name, fn = engines[next_idx]
                next_idx += 1
                pending[self._io_executor.submit(fn)] = name
                deadline = max(deadline, time.monotonic() + self.ENGINE_TIMEOUT_SECONDS)

        launch_next()
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_for = min(remaining, self.ENGINE_HEDGE_SECONDS) if next_idx < len(engines) else remaining
            done, _ = wait(list(pending), timeout=wait_for, return_when=FIRST_COMPLETED)
            if not done:
                launch_next()
                continue
            for fut in done:
                name = pending.pop(fut)
                try:
                    text = fut.result()
                except Exception as e:
                    logging.debug(f"Motore STT {name} fallito: {e}")
                    text = None
                if text:
                    return name, text
                launch_next()
        return None, None

    def _recognize_italian(self, audio: sr.AudioData, user: Optional[discord.Member] = None) -> Optional[str]:
        wav_bytes: Optional[bytes] = None
        try:
            wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
        except Exception:
            wav_bytes = None

        engines: List[Tuple[str, Callable[[], Optional[str]]]] = []
        if self.deepgram_api_key and wav_bytes:
            engines.append(("Deepgram Nova-3", partial(self._recognize_with_deepgram, wav_bytes)))
        if self.gemini_api_key and wav_bytes:
            engines.append(("Gemini Audio", partial(self._recognize_with_gemini_audio, wav_bytes)))
        if self.groq_api_key and wav_bytes:
            engines.append(("Groq Whisper-v3", partial(self._recognize_with_groq_whisper, wav_bytes, "it")))
        engines.append(("Google", partial(self._recognize_google_multi, audio, user)))

        engine_name, text = self._run_engine_cascade(engines)
        if not text:
            return None

        # Groq: secondo passaggio in inglese SOLO per le richieste di canzoni rivolte al bot
        # (prima partiva una richiesta extra per ogni frase detta nel canale)
        if (
            engine_name == "Groq Whisper-v3"
            and wav_bytes
            and self._looks_like_music_request(text)
            and self._is_command_candidate(user, text)
        ):
            en_hint = self._recognize_with_groq_whisper(wav_bytes, language="en", timeout=2.0)
            if en_hint and _fold_text(en_hint) != _fold_text(text):
                logging.info(f"[STT: Groq Whisper-v3 Dual] IT='{text}' | EN='{en_hint}'")
                return f"{text} [EN_HINT: {en_hint}]"

        if engine_name != "Google":
            logging.info(f"[STT: {engine_name}] '{text}'")
        return text

    def _recognize_google_multi(
        self, audio: sr.AudioData, user: Optional[discord.Member] = None
    ) -> Optional[str]:
        """Google Speech Recognition Multi-Alternativa (it-IT top-5 + en-US per le richieste di canzoni)."""
        recognizer = self._recognizer
        try:
            func = getattr(recognizer, "recognize_google", None)
            if not func:
                return None
            raw_it = func(audio, language=self.language, show_all=True)
            best_it = None
            alt_it: list[str] = []
            if isinstance(raw_it, dict) and "alternative" in raw_it:
                for item in raw_it["alternative"]:
                    tr = (item.get("transcript") or "").strip()
                    if tr and tr not in alt_it:
                        alt_it.append(tr)
                if alt_it:
                    best_it = alt_it[0]
            elif isinstance(raw_it, str) and raw_it.strip():
                best_it = raw_it.strip()
                alt_it = [best_it]

            if not best_it:
                return None

            # Se nella seconda/terza ipotesi c'è la parola di attivazione che mancava nella prima, promuovila!
            wake_words = [_fold_text(w) for w in self._wake_words()]

            def has_wake(candidate: str) -> bool:
                folded = _fold_text(candidate)
                return any(re.search(rf"\b{re.escape(w)}\b", folded) for w in wake_words if w)

            if not has_wake(best_it):
                for cand in alt_it[1:]:
                    if has_wake(cand):
                        best_it = cand
                        break

            extra_hints = []
            if len(alt_it) > 1:
                others = [a for a in alt_it if a.lower() != best_it.lower()][:3]
                if others:
                    extra_hints.extend(others)

            hinted = f"{best_it} [EN_HINT: {' | '.join(extra_hints)}]" if extra_hints else best_it
            if (
                self._looks_like_music_request(best_it)
                or any(self._looks_like_music_request(a) for a in alt_it)
            ) and self._is_command_candidate(user, hinted):
                en_clean = self._recognize_google_en(recognizer, audio)
                if en_clean and en_clean.lower() != best_it.lower():
                    extra_hints.insert(0, en_clean)

            if extra_hints:
                hint_str = " | ".join(extra_hints)
                logging.info(
                    f"[STT: Google Multi-Hyp] IT='{best_it}' | HINTS='{hint_str}'"
                )
                return f"{best_it} [EN_HINT: {hint_str}]"

            logging.info(f"[STT: Google] '{best_it}'")
            return best_it
        except sr.UnknownValueError:
            pass
        except sr.RequestError as e:
            logging.warning(f"Errore richiesta Google Speech Recognition: {e}")
        except Exception as e:
            logging.debug(f"Errore trascrizione vocale: {e}")

        return None

    @staticmethod
    def _recognize_google_en(recognizer: sr.Recognizer, audio: sr.AudioData) -> Optional[str]:
        try:
            func = getattr(recognizer, "recognize_google", None)
            if not func:
                return None
            raw_en = func(audio, language="en-US", show_all=True)
            if isinstance(raw_en, dict) and "alternative" in raw_en:
                alts = [
                    (item.get("transcript") or "").strip()
                    for item in raw_en["alternative"]
                    if (item.get("transcript") or "").strip()
                ]
                return alts[0] if alts else None
            if isinstance(raw_en, str) and raw_en.strip():
                return raw_en.strip()
        except Exception:
            pass
        return None

    def _recognize_with_deepgram(self, wav_bytes: bytes) -> Optional[str]:
        try:
            resp = self._http.post(
                "https://api.deepgram.com/v1/listen?model=nova-3&language=multi&smart_format=true",
                headers={
                    "Authorization": f"Token {self.deepgram_api_key}",
                    "Content-Type": "audio/wav",
                },
                data=wav_bytes,
                timeout=3.5,
            )
            if resp.status_code == 200:
                data = resp.json()
                channels = data.get("results", {}).get("channels", [])
                if channels:
                    alts = channels[0].get("alternatives", [])
                    if alts:
                        transcript = (alts[0].get("transcript") or "").strip()
                        if transcript:
                            return transcript
        except Exception as e:
            logging.debug(f"Deepgram STT fallback: {e}")
        return None

    def _recognize_with_gemini_audio(self, wav_bytes: bytes) -> Optional[str]:
        b64_audio = base64.b64encode(wav_bytes).decode("ascii")
        wake_word = self._wake_words()[0].capitalize()
        prompt = (
            "Trascrivi fedelmente questo comando vocale in italiano. "
            f"La parola di attivazione dell'assistente è '{wake_word}': se la senti scrivila esattamente così. "
            f"Se l'utente pronuncia il titolo di una canzone o di un artista in inglese o italiano "
            f"(es. dopo '{wake_word} metti...'), scrivi il titolo e l'artista con lo spelling corretto. "
            "Restituisci SOLO le parole pronunciate dall'utente, senza commenti né punteggiatura finale."
        )
        for model in list(self._gemini_models):
            generation_config: Dict[str, Any] = {"temperature": 0.0, "maxOutputTokens": 60}
            if model not in self._gemini_no_thinking_cfg:
                # Il "thinking" aggiunge secondi di latenza: per una trascrizione non serve
                generation_config["thinkingConfig"] = {"thinkingBudget": 0}
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                            {"inlineData": {"mimeType": "audio/wav", "data": b64_audio}},
                        ]
                    }
                ],
                "generationConfig": generation_config,
            }
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={self.gemini_api_key}"
            )
            try:
                resp = self._http.post(url, json=payload, timeout=3.8)
                if resp.status_code == 400 and "thinkingConfig" in generation_config:
                    self._gemini_no_thinking_cfg.add(model)
                    generation_config.pop("thinkingConfig", None)
                    resp = self._http.post(url, json=payload, timeout=3.8)

                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        texts = [p.get("text") for p in parts if p.get("text") and not p.get("thought")]
                        text = "".join(texts).strip().strip("\"'`.")
                        if text and len(text) <= 160:
                            return text
                    return None
                if resp.status_code == 404 and len(self._gemini_models) > 1:
                    # Modello dismesso: rimuovilo così le prossime frasi non lo riprovano
                    try:
                        self._gemini_models.remove(model)
                    except ValueError:
                        pass
                    continue
                return None
            except Exception as e:
                logging.debug(f"Gemini Audio STT fallback: {e}")
                return None
        return None

    def _whisper_prompt(self, language: Optional[str]) -> str:
        names = [w.capitalize() for w in self._wake_words()[:3]]
        if language == "it":
            return f"{', '.join(names)}, metti, riproduci, pausa, riprendi, salta, stop, volume."
        return f"{names[0]}, metti"

    @staticmethod
    def _is_whisper_hallucination(text: str, segments: Optional[list], prompt: str) -> bool:
        norm = _fold_text(text)
        if not norm or norm in _WHISPER_HALLUCINATIONS or norm.startswith("sottotitoli"):
            return True
        prompt_norm = _fold_text(prompt)
        if norm == prompt_norm:
            return True  # eco del prompt

        prompt_words = set(prompt_norm.split())
        words = norm.split()
        only_prompt_words = len(words) <= 2 and all(w in prompt_words for w in words)

        if segments:
            def _f(seg: dict, key: str, default: float) -> float:
                try:
                    return float(seg.get(key, default))
                except (TypeError, ValueError):
                    return default

            # Regola standard di Whisper: segmento silenzioso se probabilità di "no speech" alta e confidenza bassa
            if all(
                _f(seg, "no_speech_prob", 0.0) > 0.6 and _f(seg, "avg_logprob", 0.0) < -1.0
                for seg in segments
            ):
                return True
            # "Venus." da solo è valido (l'utente chiama l'assistente e aspetta), ma solo se Whisper è
            # sicuro che ci fosse davvero una voce: evita attivazioni fantasma su tosse e rumori.
            if only_prompt_words:
                max_no_speech = max(_f(seg, "no_speech_prob", 0.0) for seg in segments)
                min_logprob = min(_f(seg, "avg_logprob", 0.0) for seg in segments)
                return max_no_speech > 0.4 or min_logprob < -0.9
            return False

        # Senza metadati di confidenza mantieni il comportamento prudente
        return only_prompt_words

    def _recognize_with_groq_whisper(
        self, wav_bytes: bytes, language: Optional[str] = "it", timeout: float = 3.8
    ) -> Optional[str]:
        # Prompt minimale non inquinante (con la parola di attivazione configurata):
        # evita che Whisper allucini titoli di canzoni non pronunciati!
        clean_prompt = self._whisper_prompt(language)

        for w_model in self._GROQ_WHISPER_MODELS:
            try:
                form_data = {
                    "model": w_model,
                    "prompt": clean_prompt,
                    "response_format": "verbose_json",
                    "temperature": "0.0",
                }
                if language:
                    form_data["language"] = language

                resp = self._http.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.groq_api_key}"},
                    files={"file": ("speech.wav", io.BytesIO(wav_bytes), "audio/wav")},
                    data=form_data,
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    text = (payload.get("text") or "").strip()
                    if not text or self._is_whisper_hallucination(text, payload.get("segments"), clean_prompt):
                        return None
                    return text
                elif resp.status_code in (401, 403, 404):
                    logging.debug(f"Groq Whisper ({w_model}) HTTP {resp.status_code}: {resp.text[:120]}")
                    continue
                else:
                    break
            except Exception as e:
                logging.debug(f"Groq Whisper STT ({w_model}) errore: {e}")
                break
        return None

    @voice_recv.AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, ssrc: Optional[int]) -> None:
        if member is not None:
            with self._lock:
                self._user_buffers.pop(member.id, None)

    def cleanup(self) -> None:
        self._running = False
        if hasattr(self, "_lock") and hasattr(self, "_user_buffers"):
            with self._lock:
                self._user_buffers.clear()
        for pool in (getattr(self, "_executor", None), getattr(self, "_io_executor", None)):
            try:
                if pool is not None:
                    pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        try:
            if getattr(self, "_http", None) is not None:
                self._http.close()
        except Exception:
            pass
