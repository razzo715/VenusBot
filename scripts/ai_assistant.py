import asyncio
from collections import OrderedDict, defaultdict, deque
from datetime import datetime
import json
import logging
import os
import re
import unicodedata
from typing import Any, Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from pathlib import Path


ITALIAN_MONTHS = [
    "",
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
]

ITALIAN_WEEKDAYS = [
    "lunedì",
    "martedì",
    "mercoledì",
    "giovedì",
    "venerdì",
    "sabato",
    "domenica",
]


def _build_italian_numbers(max_value: int = 150) -> Dict[str, int]:
    """
    Genera tutti i numeri italiani scritti in lettere da 0 a 150 (es. 'trentadue', 'ventotto',
    'centoventi'), così il volume si può impostare a voce con qualsiasi valore e non solo a multipli di 5.
    """
    units = ["zero", "uno", "due", "tre", "quattro", "cinque", "sei", "sette", "otto", "nove"]
    teens = [
        "dieci", "undici", "dodici", "tredici", "quattordici",
        "quindici", "sedici", "diciassette", "diciotto", "diciannove",
    ]
    tens = {
        20: "venti", 30: "trenta", 40: "quaranta", 50: "cinquanta",
        60: "sessanta", 70: "settanta", 80: "ottanta", 90: "novanta",
    }
    words: Dict[str, int] = {w: n for n, w in enumerate(units)}
    words.update({w: 10 + i for i, w in enumerate(teens)})
    for value, word in tens.items():
        words[word] = value
        for u in range(1, 10):
            # 'venti' + 'uno' -> 'ventuno', 'trenta' + 'otto' -> 'trentotto'
            stem = word[:-1] if u in (1, 8) else word
            words[stem + units[u]] = value + u

    below_hundred = dict(words)
    words["cento"] = 100
    for word, value in below_hundred.items():
        if 1 <= value and 100 + value <= max_value:
            words["cento" + word] = 100 + value
            if word[0] in "ou":
                words["cent" + word] = 100 + value  # 'centotto', 'centuno'

    # Espressioni colloquiali già supportate in precedenza
    words.update({"meta": 50, "massimo": 100, "minimo": 15})
    return words


ITALIAN_WORD_NUMBERS = _build_italian_numbers()

# Correzioni fonetiche istantanee in locale (0ms)
LOCAL_PHONETIC_FIXES = {
    "blanding laits": "The Weeknd - Blinding Lights",
    "blinding laits": "The Weeknd - Blinding Lights",
    "blanding light": "The Weeknd - Blinding Lights",
    "biondi light": "The Weeknd - Blinding Lights",
    "starboi": "The Weeknd - Starboy",
    "boemian rapsodi": "Queen - Bohemian Rhapsody",
    "bohemian rapsodi": "Queen - Bohemian Rhapsody",
    "smels laik tin spirit": "Nirvana - Smells Like Teen Spirit",
    "fein": "Travis Scott - FE!N",
    "fin": "Travis Scott - FE!N",
    "ander presciur": "Queen - Under Pressure",
    "hotel california": "Eagles - Hotel California",
    "biliv": "Cher - Believe",
    "auan mor taim": "Daft Punk - One More Time",
    "get lachi": "Daft Punk - Get Lucky",
    "sceip of iu": "Ed Sheeran - Shape of You",
    "shape of iu": "Ed Sheeran - Shape of You",
    "beliver": "Imagine Dragons - Believer",
    "ander de brig": "Red Hot Chili Peppers - Under the Bridge",
    "nam": "Linkin Park - Numb",
    "in de end": "Linkin Park - In the End",
}

# Ordine di preferenza: modelli che capiscono bene l'italiano e conoscono titoli/artisti,
# ma restano velocissimi su Groq. Sovrascrivibile con GROQ_MODEL nel file .env.
PREFERRED_GROQ_CHAT_MODELS = [
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b",
]

GEMINI_CHAT_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.0-flash"]

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"


def get_italian_datetime_str() -> str:
    try:
        now = datetime.now(ZoneInfo("Europe/Rome"))
    except Exception:
        now = datetime.now()
    weekday = ITALIAN_WEEKDAYS[now.weekday()]
    month = ITALIAN_MONTHS[now.month]
    return f"{weekday} {now.day} {month} {now.year}, ore {now.strftime('%H:%M')}"


def split_en_hint(text: str) -> Tuple[str, Optional[str]]:
    """Separa eventuale [EN_HINT: ...] aggiunto dal doppio riconoscitore vocale it-IT + en-US."""
    m = re.search(r"\[EN_HINT:\s*(.+?)\]$", text.strip(), flags=re.IGNORECASE)
    if m:
        en_hint = m.group(1).strip()
        it_text = text[: m.start()].strip()
        return it_text, en_hint
    return text.strip(), None


def _fold_char(ch: str) -> str:
    base = unicodedata.normalize("NFD", ch)[0]
    low = base.lower()
    return low if len(low) == 1 else base


def fold_text(text: str) -> str:
    """
    Minuscolo e senza accenti ('Più' -> 'piu', 'è' -> 'e') mantenendo ESATTAMENTE la stessa lunghezza,
    così le posizioni trovate sul testo normalizzato valgono anche sul testo originale.
    """
    return "".join(_fold_char(ch) for ch in text)


def clean_spoken_text(text: str) -> str:
    """
    Rimuove la punteggiatura aggiunta dai motori Speech-to-Text (Whisper scrive sempre virgole e punti:
    "Metti in pausa, per favore.") mantenendo maiuscole, accenti e apostrofi.
    """
    s, _ = split_en_hint(text)
    s = unicodedata.normalize("NFC", s)
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'").replace("´", "'")
    s = re.sub(r"[,;:!?¿¡\"“”«»…()\[\]{}*_~]+", " ", s)
    s = re.sub(r"\.+(?=\s|$)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Intercalari e formule di cortesia (su testo già normalizzato con fold_text)
_FILLER_WORDS = (
    r"ehm+|eh+|ehi|ok(?:ay|ei)?|allora|dai|senti|ascolta|scusa(?:mi)?|ma|e|beh|bene|si|no|ecco"
)
_LEADING_FILLERS_RE = re.compile(
    rf"(?:{_FILLER_WORDS}|per\s+(?:favore|piacere|cortesia)|perfavore|cortesemente|ti\s+prego(?:\s+di)?"
    r"|mi\s+(?:puoi|potresti)|puoi|potresti|riesci\s+a|vorrei\s+che|voglio\s+che)\s+"
)
_TRAILING_FILLERS_RE = re.compile(
    r"\s+(?:per\s+(?:favore|piacere|cortesia)|perfavore|grazie(?:\s+(?:mille|tante))?"
    r"|adesso|ora|subito|dai|ok|okay|please|su\s+youtube|su\s+spotify)$"
)
_ONLY_FILLERS_RE = re.compile(rf"(?:{_FILLER_WORDS})(?:\s+(?:{_FILLER_WORDS}))*")


def strip_fillers(text: str) -> str:
    """Toglie intercalari iniziali ("allora", "senti", "puoi") e cortesie finali ("per favore", "grazie")."""
    folded = fold_text(text)
    start, end = 0, len(text)
    for _ in range(3):
        m = _TRAILING_FILLERS_RE.search(folded, 0, end)
        if not m:
            break
        end = m.start()
    for _ in range(4):
        m = _LEADING_FILLERS_RE.match(folded, start, end)
        if not m:
            break
        start = m.end()
    return text[start:end].strip()


def _normalize_command_text(text: str) -> str:
    """
    Normalizza il testo per il riconoscimento locale istantaneo (< 1ms, senza LLM):
    niente punteggiatura, niente accenti, minuscolo, senza intercalari e formule di cortesia.
    """
    s = strip_fillers(clean_spoken_text(text))
    s = fold_text(s).replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_italian_number(token: str) -> Optional[int]:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return ITALIAN_WORD_NUMBERS.get(fold_text(token))


# ---------------------------------------------------------------------------
# Comandi locali istantanei: (azione, regex su testo normalizzato, risposta vocale)
# ---------------------------------------------------------------------------
_WAIT = r"(?:un\s+)?(?:attimo|attimino|secondo|momento)"
_CANCEL = (
    r"(?:niente|nulla|lascia\s+(?:stare|perdere)|annulla|non\s+importa|non\s+fa\s+niente"
    r"|ho\s+sbagliato|scusa|no|ok|okay|va\s+bene|d'\s?accordo)"
)
_BIT = r"(?:un\s+(?:po'?|pochino|pelo)|tanto|ancora|di\s+(?:piu|meno))"
_VOLUME_OBJ = r"(?:(?:il\s+)?volume|(?:la\s+)?musica|la\s+voce|l'\s?audio)"
_NEXT = r"(?:prossima|prossimo|successiva|successivo)"
_TRACK = r"(?:canzone|brano|traccia|pezzo)"
_PLAY_VERBS = (
    r"riproduci(?:mi)?|riprodurre|metti(?:mi|ci)?(?:\s+su)?|mettere(?:\s+su)?|suona(?:mi)?|suonare"
    r"|fai\s+partire|fammi\s+(?:ascoltare|sentire)|facci\s+sentire|(?:voglio|vorrei)\s+(?:ascoltare|sentire)"
    r"|ascoltiamo|sentiamo|cerca(?:mi)?(?!\s+(?:su\s+(?:google|internet|wikipedia)|informazioni))|play|avvia"
)

_FAST_COMMANDS: List[Tuple[str, "re.Pattern[str]", str]] = [
    # Annulla ("Venus... niente", "lascia stare"): nessuna risposta e nessuna chiamata AI
    ("none", re.compile(rf"{_CANCEL}(?:\s+{_CANCEL})*"), ""),
    # "Venus, metti" / "Venus, riproduci" + pausa prima del titolo: apri la finestra di ascolto del brano
    ("listen_song_followup", re.compile(
        rf"(?:{_PLAY_VERBS})(?:\s+(?:una|un|qualche)\s+{_TRACK}|\s+(?:un\s+po'?\s+di\s+|della\s+)?musica)?"
    ), "Dimmi il titolo."),
    ("pause", re.compile(
        r"(?:metti|mettere|fai|facciamo|fare)\s+(?:in\s+|una\s+)?pausa(?:\s+(?:alla|la)\s+(?:musica|canzone))?"
        r"|(?:metti|mettere)\s+(?:la\s+musica|la\s+canzone|il\s+brano)\s+in\s+pausa"
        r"|(?:pausa|pause)(?:\s+(?:alla\s+|la\s+)?(?:musica|canzone))?"
        rf"|(?:ferma(?:ti)?|stoppa|blocca)(?:\s+(?:la\s+musica|la\s+canzone))?\s+{_WAIT}"
        rf"|aspetta(?:\s+{_WAIT})?|un\s+(?:attimo|momento|secondo)"
    ), "Musica in pausa."),
    ("replay", re.compile(
        r"(?:ricomincia(?:re|la)?|riparti(?:re)?|rimetti(?:la)?|rimettere|torna(?:re)?|ripeti)"
        r"(?:\s+(?:la\s+canzone|il\s+brano|questa\s+canzone))?\s+(?:da\s+capo|dall'?\s?inizio|all'?\s?inizio)"
        r"|ricomincia(?:re|la)?(?:\s+(?:la\s+canzone|il\s+brano))?"
        r"|rimetti\s+(?:la\s+canzone|il\s+brano)|da\s+capo|dall'?\s?inizio|replay|restart"
    ), "Rimetto la canzone dall'inizio."),
    ("resume", re.compile(
        r"(?:riprendi|riprendere|riparti|ripartire|fai\s+ripartire|continua|continuare|resume|unpause"
        r"|(?:togli|leva)\s+(?:la\s+)?pausa)"
        r"(?:\s+(?:la\s+)?(?:musica|riproduzione|canzone|brano)|\s+a\s+suonare|\s+pure)?"
        r"|rimetti\s+(?:su\s+)?(?:la\s+)?musica"
    ), "Riprendo la musica."),
    ("skip", re.compile(
        r"(?:salta|saltare|skippa|skippare|passa|passare|cambia|cambiare)(?:\s+(?:questa|questo))?"
        rf"(?:\s+(?:la\s+|il\s+)?(?:{_TRACK}|musica))?(?:\s+(?:alla|al)\s+{_NEXT}(?:\s+{_TRACK})?)?"
        rf"|skip|next|avanti|vai\s+avanti|{_NEXT}"
        rf"|(?:vai|passa|metti|manda)\s+(?:alla|la|al|il)\s+{_NEXT}(?:\s+{_TRACK})?"
        rf"|(?:la\s+|il\s+)?{_NEXT}\s+{_TRACK}|{_TRACK}\s+(?:successiva|successivo|dopo)|la\s+prossima|il\s+prossimo"
    ), "Salto il brano."),
    ("clear_queue", re.compile(
        r"(?:svuota|svuotare|cancella|cancellare|pulisci|pulire|elimina|eliminare|resetta|resettare|togli)"
        r"\s+(?:la\s+|tutta\s+la\s+)?coda(?:\s+di\s+riproduzione)?"
    ), "Ho svuotato la coda di riproduzione."),
    ("stop", re.compile(
        r"(?:stop|stoppa(?:re)?|ferma(?:re|ti)?|spegni(?:re)?|smetti(?:la)?|basta)"
        r"(?:\s+(?:tutto|(?:la\s+|alla\s+)?musica|la\s+riproduzione|la\s+canzone|di\s+suonare|canzoni|cosi))?"
    ), "Fermo la musica e svuoto la coda."),
    ("autoplay", re.compile(
        r"(?:(?:attiva|attivare|disattiva|disattivare|metti|togli)\s+(?:l'\s?)?autoplay|autoplay"
        r"|(?:(?:attiva|disattiva|togli)\s+(?:la\s+)?)?riproduzione\s+automatica)"
    ), ""),
    ("loop", re.compile(
        r"(?:metti|mettere|attiva|attivare)\s+(?:in\s+|il\s+)?loop|(?:togli|togliere|disattiva|disattivare|leva)\s+(?:il\s+)?loop|loop"
        r"|(?:ripeti|ripetere|ripetila)(?:\s+(?:questa\s+canzone|la\s+canzone|canzone|il\s+brano|brano|questa|questo))?"
        r"(?:\s+(?:sempre|all'\s?infinito|in\s+loop|a\s+ripetizione))?"
        r"|(?:(?:attiva|disattiva|togli|metti)\s+(?:la\s+)?)?ripetizione|a\s+ripetizione"
    ), ""),
    ("shuffle", re.compile(
        r"(?:mescola|mescolare|mischia|mischiare|rimescola)(?:\s+(?:la\s+coda|le\s+canzoni|i\s+brani|la\s+playlist))?"
        r"|shuffle|(?:metti\s+)?(?:la\s+)?riproduzione\s+casuale|(?:in\s+)?ordine\s+casuale|casuale"
    ), "Ho mescolato la coda di riproduzione."),
    ("volume_up", re.compile(
        rf"(?:alza|alzare|aumenta|aumentare|alzami|sali)(?:\s+{_BIT})?(?:\s+{_VOLUME_OBJ})?(?:\s+{_BIT})?"
        r"|(?:(?:un\s+)?po'?\s+)?piu\s+(?:forte|alto)|volume\s+(?:su|piu\s+alto|piu\s+forte)"
        r"|non\s+(?:si\s+)?sente(?:\s+(?:niente|bene))?"
    ), "Alzo il volume."),
    ("volume_down", re.compile(
        rf"(?:abbassa|abbassare|diminuisci|diminuire|riduci|ridurre|abbassami|scendi)(?:\s+{_BIT})?(?:\s+{_VOLUME_OBJ})?(?:\s+{_BIT})?"
        r"|(?:(?:un\s+)?po'?\s+)?piu\s+(?:piano|basso)|volume\s+(?:giu|piu\s+basso)|(?:e\s+)?troppo\s+(?:forte|alto)"
    ), "Abbasso il volume."),
    ("now_playing", re.compile(
        r"(?:che|quale)\s+(?:canzone|brano|musica|pezzo)\s+(?:e(?:\s+(?:questa|questo))?|sta\s+(?:suonando|andando)"
        r"|stai\s+(?:suonando|mettendo|riproducendo)|hai\s+messo|e\s+in\s+riproduzione)"
        r"|(?:che\s+)?cos'?\s?e\s+(?:questa|questo)(?:\s+(?:canzone|brano|pezzo))?"
        r"|cosa\s+(?:sta|stai)\s+(?:suonando|andando|riproducendo|mettendo)"
        r"|come\s+si\s+chiama\s+(?:questa\s+canzone|questo\s+brano|questo\s+pezzo|la\s+canzone|questa|questo)"
        r"|chi\s+canta(?:\s+(?:questa|questo)(?:\s+(?:canzone|brano|pezzo))?)?"
        r"|(?:di\s+)?chi\s+e\s+(?:questa|questo)(?:\s+(?:canzone|brano|pezzo))?|titolo(?:\s+della\s+canzone)?"
    ), ""),
    ("disconnect", re.compile(
        r"(?:esci|uscire|esca)(?:\s+(?:dal\s+canale|dal\s+vocale|dalla\s+(?:vocale|chiamata|stanza)))?"
        r"|disconnettiti|disconnetti|vai\s+via|vattene|lascia\s+(?:il\s+canale|la\s+(?:vocale|chiamata))"
    ), "A presto!"),
]

_RADIO_VERBS = r"(?:metti|riproduci|avvia|suona|accendi)\s+(?:la\s+)?(?:radio\s+)?"
_RADIO_COMMANDS: List[Tuple["re.Pattern[str]", str, str]] = [
    (re.compile(rf"{_RADIO_VERBS}lo\s?fi\s+jazz"), "https://www.youtube.com/watch?v=HuFYqnbVbzY", "Avvio la radio Lofi Jazz."),
    (re.compile(rf"{_RADIO_VERBS}lo\s?fi"), "https://www.youtube.com/watch?v=jfKfPfyJRdk", "Avvio la radio Lofi."),
    (re.compile(rf"{_RADIO_VERBS}synth\s?wave"), "https://www.youtube.com/watch?v=4xDzrJKXOOY", "Avvio la radio Synthwave."),
]

_VOLUME_SET_RE = re.compile(
    r"(?:(?P<verb>imposta(?:re)?|metti|mettere|porta(?:re)?|regola(?:re)?|setta(?:re)?|alza(?:re)?|abbassa(?:re)?)\s+)?"
    r"(?:(?:il|al)\s+)?(?P<vol>volume\s+)?(?:(?:a|al|allo|alla|su|fino\s+al?)\s+)?"
    r"(?P<num>\d{1,3}|[a-z]+)(?:\s*(?:%|percento|per\s+cento))?"
)
_VOLUME_VERBS_WITHOUT_NOUN = ("alza", "abbassa", "porta", "regola", "imposta", "setta")

_TIME_RE = re.compile(
    r"che\s+ore\s+sono|che\s+ora\s+(?:e|si\s+e|fa)|(?:dimmi|mi\s+dici|sai)\s+(?:l'\s?ora|che\s+ore\s+sono|che\s+ora\s+e)"
    r"|ora\s+esatta"
)
_DATE_RE = re.compile(
    r"(?:oggi\s+)?che\s+giorno\s+(?:e|siamo)(?:\s+oggi)?|in\s+che\s+giorno\s+siamo|quanti\s+ne\s+abbiamo(?:\s+oggi)?"
    r"|che\s+data\s+(?:e|abbiamo)(?:\s+oggi)?|(?:dimmi|mi\s+dici)\s+(?:la\s+data|che\s+giorno\s+e)(?:\s+di\s+oggi)?"
)
_CHAT_REPLIES: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("listen_followup", re.compile(r"ciao|ehi|hey|ei|ehila|buongiorno|buonasera|buon\s+pomeriggio|salve"), "Ciao! Dimmi pure."),
    ("none", re.compile(r"come\s+stai|tutto\s+bene|come\s+va|come\s+te\s+la\s+passi"), "Tutto benissimo, grazie!"),
    ("listen_followup", re.compile(
        r"mi\s+senti|ci\s+sei|sei\s+attiva|sei\s+li|sei\s+sveglia|mi\s+ascolti|funzioni|stai\s+ascoltando"
    ), "Sì, ti sento! Dimmi pure."),
    ("none", re.compile(
        r"grazie(?:\s+(?:mille|tante|ancora))?|perfetto|ottimo|brava|bravissima|grande|ok\s+grazie|molto\s+bene"
    ), "Di nulla!"),
]

_PLAY_VERB_RE = re.compile(rf"(?:{_PLAY_VERBS})\s+")
_PLAY_OBJECT_PREFIX_RE = re.compile(
    r"(?:(?:la\s+canzone|il\s+brano|il\s+pezzo|le\s+canzoni)(?:\s+(?:di|dei|degli|delle|del|della))?"
    r"|(?:un\s+brano|una\s+canzone|un\s+pezzo|qualcosa|un\s+po'?)\s+(?:di|dei|degli|delle|del|della|dello)"
    r"|(?:la\s+)?musica\s+(?:di|dei|degli|delle))\s+"
)


class AssistantResponse:
    def __init__(
        self,
        action: str = "none",
        query: str = "",
        voice_reply: str = "",
    ):
        self.action = (action or "none").strip().lower()
        self.query = (query or "").strip()
        self.voice_reply = (voice_reply or "").strip()

    def to_dict(self) -> Dict[str, str]:
        return {
            "action": self.action,
            "query": self.query,
            "voice_reply": self.voice_reply,
        }


class AIAssistant:
    """
    Motore Ibrido per VenusBot:
    1. Comandi di controllo (pausa, salta, stop, volume, loop, shuffle, autoplay, replay, ora/data):
       100% LOCALI in < 1ms (zero chiamate di rete!), tolleranti a punteggiatura, accenti,
       intercalari ("allora", "senti", "puoi") e formule di cortesia.
    2. Richieste di riproduzione musicale ("metti <canzone>"):
       Usa la doppia trascrizione (Italiano + Inglese) e un micro-risolutore fonetico con cache
       per identificare con precisione qualsiasi canzone italiana o inglese.
    """

    SONG_CACHE_SIZE = 256

    def __init__(self):
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        self.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip().strip("'\"")
        self.gemini_api_key = (os.getenv("GEMINI_API_KEY") or "").strip().strip("'\"")
        self.custom_groq_model = (os.getenv("GROQ_MODEL") or "").strip()
        self.gemini_models: List[str] = []
        for model in [(os.getenv("GEMINI_MODEL") or "").strip(), *GEMINI_CHAT_MODELS]:
            if model and model not in self.gemini_models:
                self.gemini_models.append(model)

        self._discovered_groq_models: Optional[List[str]] = None
        self._groq_plain_models: set = set()  # modelli che rifiutano JSON mode / parametri di reasoning
        self._gemini_no_thinking_cfg: set = set()
        self._song_cache: "OrderedDict[str, str]" = OrderedDict()
        self._history: Dict[int, Deque[Dict[str, str]]] = defaultdict(lambda: deque(maxlen=10))
        self._http_session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            # keepalive più lungo del default (15s): niente nuovo handshake TLS a ogni comando vocale
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=6),
                connector=aiohttp.TCPConnector(keepalive_timeout=60),
            )
        return self._http_session

    async def prewarm(self) -> None:
        """Scopre i modelli Groq e apre la connessione HTTPS in anticipo (prima richiesta vocale più veloce)."""
        if not self.groq_api_key:
            return
        try:
            session = await self._get_session()
            await self._get_groq_candidate_models(session)
        except Exception as e:
            logging.debug(f"Prewarm assistente AI non riuscito: {e}")

    def clear_history(self, guild_id: int) -> None:
        self._history.pop(guild_id, None)

    def _match_fast_command(self, text: str) -> Optional[AssistantResponse]:
        """
        Elaborazione 100% LOCALE (< 1ms, zero LLM) per tutti i comandi di controllo
        del player, regolazione volume, radio 24/7, orario, data e saluti.
        """
        lower = _normalize_command_text(text)

        if not lower:
            return AssistantResponse(action="listen_followup", voice_reply="")

        for action, pattern, reply in _FAST_COMMANDS:
            if pattern.fullmatch(lower):
                return AssistantResponse(action=action, voice_reply=reply)

        m_vol = _VOLUME_SET_RE.fullmatch(lower)
        if m_vol:
            value = parse_italian_number(m_vol.group("num"))
            has_context = bool(m_vol.group("vol")) or any(
                (m_vol.group("verb") or "").startswith(v) for v in _VOLUME_VERBS_WITHOUT_NOUN
            )
            if value is not None and has_context:
                vol = max(5, min(150, value))
                return AssistantResponse(
                    action="volume_set",
                    query=str(vol),
                    voice_reply=f"Volume impostato al {vol} percento.",
                )

        for pattern, url, reply in _RADIO_COMMANDS:
            if pattern.fullmatch(lower):
                return AssistantResponse(action="play", query=url, voice_reply=reply)

        if _TIME_RE.fullmatch(lower):
            try:
                now = datetime.now(ZoneInfo("Europe/Rome"))
            except Exception:
                now = datetime.now()
            return AssistantResponse(
                action="none",
                voice_reply=f"Sono le {now.strftime('%H e %M')}.",
            )

        if _DATE_RE.fullmatch(lower):
            return AssistantResponse(
                action="none",
                voice_reply=f"Oggi è {get_italian_datetime_str().split(',')[0]}.",
            )

        for action, pattern, reply in _CHAT_REPLIES:
            if pattern.fullmatch(lower):
                return AssistantResponse(action=action, voice_reply=reply)

        # Solo intercalari ("Venus... ehm, allora"): resta in ascolto senza rispondere
        if _ONLY_FILLERS_RE.fullmatch(lower):
            return AssistantResponse(action="listen_followup", voice_reply="")

        return None

    @staticmethod
    def _extract_Explicit_play_query(text: str) -> Optional[str]:
        """Estrae la canzone/artista da 'metti/riproduci/suona/play ...' preservando le maiuscole."""
        clean = strip_fillers(clean_spoken_text(text))
        m_play = _PLAY_VERB_RE.match(fold_text(clean))
        if not m_play:
            return None
        q = clean[m_play.end():].strip()
        m_obj = _PLAY_OBJECT_PREFIX_RE.match(fold_text(q))
        if m_obj and m_obj.end() < len(q):
            q = q[m_obj.end():].strip()
        return q or None

    def _cache_song(self, key: str, value: str) -> None:
        self._song_cache[key] = value
        self._song_cache.move_to_end(key)
        while len(self._song_cache) > self.SONG_CACHE_SIZE:
            self._song_cache.popitem(last=False)

    async def resolve_song_query(self, raw_query: str, en_hint: Optional[str] = None) -> str:
        """
        Risolutore fonetico ultra-veloce specifico per i titoli delle canzoni:
        - Se il titolo è nel dizionario fonetico locale o è già stato risolto, risponde in 0ms.
        - Altrimenti usa una micro-chiamata AI (max_tokens=40, temperature=0.0) combinando
          sia la trascrizione italiana sia l'eventuale trascrizione inglese (EN_HINT)
          per ricostruire il vero 'Artista - Titolo Canzone'.
        """
        clean_q = raw_query.strip()
        if not clean_q:
            return clean_q

        lookup_key = re.sub(r"[^\w' ]+", "", fold_text(clean_q)).strip()
        if lookup_key in LOCAL_PHONETIC_FIXES:
            return LOCAL_PHONETIC_FIXES[lookup_key]

        cache_key = f"{lookup_key}|{fold_text(en_hint or '')}"
        if cache_key in self._song_cache:
            self._song_cache.move_to_end(cache_key)
            return self._song_cache[cache_key]

        # Se non ci sono chiavi AI, usa l'hint inglese (se presente) o la query pulita
        if not self.groq_api_key and not self.gemini_api_key:
            if en_hint:
                en_extracted = self._extract_Explicit_play_query(en_hint)
                if en_extracted:
                    return en_extracted
            return clean_q

        hint_line = f"\nTrascrizione fonetica alternativa (en-US): \"{en_hint}\"" if en_hint else ""
        system_prompt = (
            "Sei un esperto musicale per un assistente vocale italiano. "
            "L'utente ha chiesto al microfono di riprodurre una canzone o un artista, ma lo speech-to-text "
            "potrebbe aver trascritto un titolo inglese o italiano con errori fonetici.\n"
            "Restituisci SOLO ED ESCLUSIVAMENTE il vero nome nel formato 'Artista - Titolo Canzone' "
            "(oppure il nome ufficiale dell'Artista se ha chiesto solo un artista, oppure un brano famoso "
            "adatto se ha chiesto un genere o un'atmosfera), senza virgolette né spiegazioni.\n"
            "Se la trascrizione è già corretta, restituiscila con lo spelling ufficiale.\n"
            "Esempi:\n"
            "- 'biondi light' -> The Weeknd - Blinding Lights\n"
            "- 'smels laik tin spirit' -> Nirvana - Smells Like Teen Spirit\n"
            "- 'boemia rapsodia' -> Queen - Bohemian Rhapsody\n"
            "- 'nam dei linkin park' -> Linkin Park - Numb\n"
            "- 'cenere di lazza' -> Lazza - Cenere\n"
            "- 'albachiara di vasco' -> Vasco Rossi - Albachiara\n"
            "- 'sapore di sale' -> Gino Paoli - Sapore di sale\n"
            "- 'una canzone rock anni 80' -> Bon Jovi - Livin' On A Prayer\n"
            "- 'geolier' -> Geolier"
        )
        user_content = f"Trascrizione vocale (it-IT): \"{clean_q}\"{hint_line}"

        resolved: Optional[str] = None
        try:
            if self.groq_api_key:
                resolved = await self._groq_chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=40,
                    temperature=0.0,
                    timeout=2.5,
                    max_models=2,
                )
            if not resolved and self.gemini_api_key:
                resolved = await self._gemini_chat(
                    system_prompt, [], user_content, max_tokens=40, temperature=0.0, timeout=3.0
                )
        except Exception as e:
            logging.debug(f"Risolutore rapido canzoni fallback: {e}")

        if resolved:
            resolved = re.sub(r"<think>.*?</think>", "", resolved, flags=re.DOTALL).strip()
            resolved = resolved.strip("\"'`.").split("\n")[0].strip()
            if resolved and len(resolved) <= 90:
                logging.info(f"[Risolutore Canzoni AI] '{clean_q}' -> '{resolved}'")
                self._cache_song(cache_key, resolved)
                return resolved

        return clean_q

    async def suggest_related_song(
        self, seed_title: str, exclude_titles: Optional[List[str]] = None
    ) -> Optional[str]:
        """
        Suggerisce tramite l'AI un brano correlato dello stesso genere/artista per la modalità Autoplay.
        """
        if not self.groq_api_key:
            return None
        excluded_str = ", ".join((exclude_titles or [])[-15:])
        messages = [
            {
                "role": "system",
                "content": (
                    "Sei un DJ radiofonico esperto. Dato un brano appena ascoltato, "
                    "consiglia UN SOLO brano famoso molto simile per stile, epoca o artista. "
                    "Rispondi ESCLUSIVAMENTE nel formato 'Artista - Titolo Canzone', senza spiegazioni."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Ultimo brano ascoltato: {seed_title}\n"
                    f"Brani già ascoltati da EVITARE: {excluded_str or 'Nessuno'}\n"
                    "Prossimo brano consigliato (Artista - Titolo):"
                ),
            },
        ]
        try:
            suggestion = await self._groq_chat(
                messages, max_tokens=35, temperature=0.6, timeout=3.0, max_models=2
            )
            if suggestion:
                suggestion = re.sub(r"<think>.*?</think>", "", suggestion, flags=re.DOTALL).strip()
                suggestion = suggestion.strip("\"'`.").split("\n")[0].strip()
                if suggestion and "-" in suggestion and len(suggestion) <= 85:
                    return suggestion
        except Exception as e:
            logging.debug(f"Errore suggest_related_song AI: {e}")
        return None

    async def _get_groq_candidate_models(self, session: aiohttp.ClientSession) -> List[str]:
        if self._discovered_groq_models:
            return self._discovered_groq_models

        candidates: List[str] = []
        if self.custom_groq_model:
            candidates.append(self.custom_groq_model)

        try:
            headers = {"Authorization": f"Bearer {self.groq_api_key}"}
            async with session.get(
                "https://api.groq.com/openai/v1/models",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    available_ids = [
                        m.get("id", "")
                        for m in (data.get("data") or [])
                        if m.get("id")
                    ]
                    excluded_keywords = (
                        "whisper",
                        "tts",
                        "playai",
                        "guard",
                        "safeguard",
                        "embed",
                        "orpheus",
                        "allam",
                        "compound",
                    )
                    chat_ids = [
                        mid
                        for mid in available_ids
                        if not any(ex in mid.lower() for ex in excluded_keywords)
                    ]

                    for pref in PREFERRED_GROQ_CHAT_MODELS:
                        if pref in chat_ids and pref not in candidates:
                            candidates.append(pref)

                    for cid in chat_ids:
                        if cid not in candidates:
                            candidates.append(cid)

                    if candidates:
                        self._discovered_groq_models = candidates
                        logging.info(f"Modelli Groq rilevati (priorità comprensione + velocità): {candidates[:3]}")
                        return candidates
        except Exception as e:
            logging.debug(f"Impossibile elencare i modelli Groq: {e}")

        for pref in PREFERRED_GROQ_CHAT_MODELS:
            if pref not in candidates:
                candidates.append(pref)
        return candidates

    @staticmethod
    def _groq_reasoning_params(model_name: str) -> Dict[str, Any]:
        """I modelli 'ragionanti' rispondono molto prima con il ragionamento ridotto al minimo."""
        m = model_name.lower()
        if "gpt-oss" in m:
            return {"reasoning_effort": "low"}
        if "qwen3" in m:
            return {"reasoning_effort": "none"}
        return {}

    async def _groq_chat(
        self,
        messages: list,
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
        json_mode: bool = False,
        max_models: int = 3,
    ) -> Optional[str]:
        """
        Chiamata chat Groq con fallback limitato tra i modelli (max_models) e timeout corto:
        un modello lento o dismesso non blocca più la risposta vocale per decine di secondi.
        """
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
        }
        session = await self._get_session()
        candidate_models = await self._get_groq_candidate_models(session)

        for model_name in list(candidate_models)[:max_models]:
            payload: Dict[str, Any] = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if model_name not in self._groq_plain_models:
                payload.update(self._groq_reasoning_params(model_name))
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}
            try:
                for attempt in range(2):
                    try:
                        async with session.post(
                            GROQ_CHAT_URL,
                            headers=headers,
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                return data["choices"][0]["message"]["content"]
                            err_text = await resp.text()
                        break
                    except aiohttp.ServerDisconnectedError:
                        # Connessione keep-alive chiusa dal server: riprova subito su una nuova
                        if attempt == 1:
                            raise

                if resp.status in (400, 404):
                    if (
                        "model_not_found" in err_text
                        or "decommissioned" in err_text
                        or "not exist" in err_text
                    ):
                        if self._discovered_groq_models and model_name in self._discovered_groq_models:
                            self._discovered_groq_models.remove(model_name)
                        continue

                    if model_name not in self._groq_plain_models and len(payload) > 4:
                        # Il modello non supporta JSON mode o i parametri di reasoning: riprova senza
                        self._groq_plain_models.add(model_name)
                        plain = {k: payload[k] for k in ("model", "messages", "temperature", "max_tokens")}
                        async with session.post(
                            GROQ_CHAT_URL,
                            headers=headers,
                            json=plain,
                            timeout=aiohttp.ClientTimeout(total=timeout),
                        ) as resp2:
                            if resp2.status == 200:
                                data2 = await resp2.json()
                                return data2["choices"][0]["message"]["content"]
                    continue
                if resp.status == 429:
                    logging.debug(f"Groq ({model_name}) rate limit raggiunto, provo il modello successivo.")
                    continue
            except Exception as e:
                logging.debug(f"Errore chiamata Groq ({model_name}): {e}")
        return None

    async def _gemini_chat(
        self,
        system_prompt: str,
        history: list,
        user_text: str,
        *,
        max_tokens: int,
        temperature: float,
        timeout: float,
        json_mode: bool = False,
    ) -> Optional[str]:
        contents = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        contents.append({"role": "user", "parts": [{"text": user_text}]})

        session = await self._get_session()
        for model_name in list(self.gemini_models):
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent?key={self.gemini_api_key}"
            )
            generation_config: Dict[str, Any] = {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
            if json_mode:
                generation_config["responseMimeType"] = "application/json"
            if model_name not in self._gemini_no_thinking_cfg:
                # Il "thinking" dei modelli Gemini recenti aggiunge secondi di attesa: disattivalo
                generation_config["thinkingConfig"] = {"thinkingBudget": 0}
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": contents,
                "generationConfig": generation_config,
            }
            try:
                for _ in range(2):
                    async with session.post(
                        url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                texts = [p.get("text") for p in parts if p.get("text") and not p.get("thought")]
                                if texts:
                                    # Il modello che risponde va in testa: le prossime richieste partono subito da lui
                                    if self.gemini_models[0] != model_name:
                                        self.gemini_models.remove(model_name)
                                        self.gemini_models.insert(0, model_name)
                                    return "".join(texts)
                            break
                        if resp.status == 400 and "thinkingConfig" in generation_config:
                            self._gemini_no_thinking_cfg.add(model_name)
                            generation_config.pop("thinkingConfig", None)
                            continue
                        if resp.status == 404 and len(self.gemini_models) > 1:
                            self.gemini_models.remove(model_name)
                        break
            except Exception as e:
                logging.debug(f"Errore chiamata Gemini ({model_name}): {e}")
        return None

    async def process_request(
        self,
        guild_id: int,
        user_name: str,
        user_text: str,
        current_song: Optional[str] = None,
        queue_count: int = 0,
        force_song_mode: bool = False,
    ) -> AssistantResponse:
        it_text, en_hint = split_en_hint(user_text)

        # 1. Prova sempre prima i comandi di controllo LOCALI ISTANTANEI (< 1ms, zero LLM!)
        fast = self._match_fast_command(it_text)
        if fast is not None:
            return fast

        # 2. Se l'utente è nella finestra dopo "Venus, metti..." (force_song_mode)
        # oppure ha pronunciato "metti / riproduci / suona <canzone>", usa il risolutore rapido canzoni!
        raw_song_query = self._extract_Explicit_play_query(it_text)
        if not raw_song_query and force_song_mode:
            raw_song_query = strip_fillers(clean_spoken_text(it_text)) or None

        if raw_song_query:
            resolved_song = await self.resolve_song_query(raw_song_query, en_hint=en_hint)
            return AssistantResponse(
                action="play",
                query=resolved_song,
                voice_reply=f"Riproduco {resolved_song}.",
            )

        # 3. Altrimenti usa l'assistente AI completo (per domande generali o se l'utente ha detto
        # direttamente il titolo di una canzone dopo aver detto solo "Venus")
        hint_ctx = f" (Trascrizione alternativa inglese: '{en_hint}')" if en_hint else ""
        system_prompt = (
            "Sei Venus, un assistente vocale intelligente in stile Google Assistant in un canale vocale Discord.\n"
            f"Data e ora attuale in Italia: {get_italian_datetime_str()}.\n"
            f"Utente: {user_name}. Brano attuale: {current_song or 'Nessuno'}. Brani in coda: {queue_count}.\n\n"
            "Il testo arriva da un riconoscimento vocale: può contenere errori fonetici, parole spezzate o "
            "omofoni. Interpreta l'intenzione più probabile invece di prendere le parole alla lettera.\n\n"
            "REGOLE:\n"
            "- Se l'utente chiede di mettere una canzone o pronuncia il titolo di un brano/artista musicale "
            "(anche con errori di trascrizione fonetica in italiano o inglese), imposta \"action\": \"play\" e scrivi "
            "in \"query\" il vero 'Artista - Titolo Canzone'.\n"
            "- Se chiede di controllare la musica (pausa, salta, volume, ecc.) usa l'azione corrispondente.\n"
            "- Se è una domanda generale, rispondi in italiano naturale e conciso con \"action\": \"none\".\n\n"
            "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido:\n"
            "{\n"
            '  "action": "none" | "play" | "pause" | "resume" | "skip" | "stop" | "replay" | "clear_queue" | "autoplay" | "loop" | "shuffle" | "volume_up" | "volume_down" | "volume_set" | "now_playing" | "disconnect",\n'
            '  "query": "Artista - Titolo Canzone (solo se action è play) oppure numero 1-100 (solo se action è volume_set), altrimenti stringa vuota",\n'
            '  "voice_reply": "Risposta parlata in italiano naturale e concisa (massimo 1-2 frasi brevi), senza asterischi o markdown."\n'
            "}"
        )

        history = list(self._history[guild_id])
        messages = [{"role": "system", "content": system_prompt}] + history + [
            {"role": "user", "content": f"{it_text}{hint_ctx}"}
        ]

        raw_reply = None

        if self.groq_api_key:
            raw_reply = await self._groq_chat(
                messages, max_tokens=200, temperature=0.3, timeout=4.0, json_mode=True, max_models=2
            )

        if not raw_reply and self.gemini_api_key:
            raw_reply = await self._gemini_chat(
                system_prompt,
                history,
                f"{it_text}{hint_ctx}",
                max_tokens=200,
                temperature=0.3,
                timeout=4.5,
                json_mode=True,
            )

        if not raw_reply:
            raw_reply = await self._call_free_cloud_fallback(messages)

        parsed = self._parse_ai_response(raw_reply, None)

        self._history[guild_id].append({"role": "user", "content": it_text})
        self._history[guild_id].append(
            {"role": "assistant", "content": parsed.voice_reply}
        )

        return parsed

    async def _call_free_cloud_fallback(self, messages: list) -> Optional[str]:
        url = "https://text.pollinations.ai/openai"
        payload = {
            "model": "openai",
            "messages": messages,
            "temperature": 0.3,
            "jsonMode": True,
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict) and "choices" in data:
                        return data["choices"][0]["message"]["content"]
                    return await resp.text()
        except Exception as e:
            logging.warning(f"Errore fallback cloud AI: {e}")
        return None

    def _parse_ai_response(
        self, raw_reply: Optional[str], explicit_song_fallback: Optional[str] = None
    ) -> AssistantResponse:
        if not raw_reply:
            if explicit_song_fallback:
                return AssistantResponse(
                    action="play",
                    query=explicit_song_fallback,
                    voice_reply=f"Riproduco {explicit_song_fallback}.",
                )
            return AssistantResponse(
                action="none",
                voice_reply="Mi dispiace, non ho capito bene.",
            )

        cleaned = raw_reply.strip()
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

        json_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if json_match:
            try:
                obj = json.loads(json_match.group(0))
                if isinstance(obj, dict):
                    action = str(obj.get("action") or "none").strip().lower()
                    query = str(obj.get("query") or "").strip()
                    voice_reply = str(obj.get("voice_reply") or obj.get("reply") or "").strip()
                    if action == "play" and not query and explicit_song_fallback:
                        query = explicit_song_fallback
                    if not voice_reply and action == "play" and query:
                        voice_reply = f"Riproduco {query}."
                    return AssistantResponse(
                        action=action,
                        query=query,
                        voice_reply=voice_reply,
                    )
            except Exception as e:
                logging.debug(f"Parsing JSON risposta AI fallito, uso testo diretto: {e}")

        if explicit_song_fallback:
            return AssistantResponse(
                action="play",
                query=explicit_song_fallback,
                voice_reply=f"Riproduco {explicit_song_fallback}.",
            )

        plain_text = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL).strip()
        return AssistantResponse(action="none", query="", voice_reply=plain_text)
