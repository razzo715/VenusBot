import difflib
import json
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import aiofiles


def _strip_accents(text: str) -> str:
    """Rimuove gli accenti mantenendo le lettere base (es. 'è' -> 'e')."""
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _fold(text: str) -> str:
    """Minuscolo e senza accenti, mantenendo la stessa lunghezza del testo originale."""
    out = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)[0]
        low = base.lower()
        out.append(low if len(low) == 1 else base)
    return "".join(out)


def _italian_phonetic_key(word: str) -> str:
    """
    Normalizza una parola secondo la fonetica italiana comune dei motori STT.
    Utile quando parole come 'venus', 'jarvis', 'alexa' vengono trascritte
    con grafie italiane o inglesi leggermente diverse.
    """
    w = _strip_accents(word.lower().strip())
    w = re.sub(r"[^a-z0-9]", "", w)
    if not w:
        return ""

    # Sostituzioni fonetiche tipiche italiano/inglese nello speech-to-text
    replacements = [
        ("gi", "j"),
        ("ge", "je"),
        ("ch", "k"),
        ("ck", "k"),
        ("c", "k"),
        ("qu", "kw"),
        ("ph", "f"),
        ("w", "v"),
        ("y", "i"),
        ("ee", "i"),
        ("oo", "u"),
        ("sh", "sc"),
    ]
    for old, new in replacements:
        w = w.replace(old, new)

    # Riduci doppie consonanti (es. "vennus" -> "venus")
    w = re.sub(r"(.)\1+", r"\1", w)
    return w


# Prefissi colloquiali italiani che spesso precedono la wake word
ITALIAN_GREETING_PREFIXES = {
    "ehi",
    "hey",
    "ei",
    "ok",
    "okay",
    "okei",
    "oh",
    "o",
    "ciao",
    "scusa",
    "senti",
    "ascolta",
    "allora",
    "bella",
    "ehila",
}
_PREFIXES_PATTERN = "|".join(sorted(map(re.escape, ITALIAN_GREETING_PREFIXES), key=len, reverse=True))

# Grafie con cui i motori STT italiani/inglesi trascrivono spesso la wake word predefinita
WAKE_WORD_VARIANTS = {
    "venus": ("venere", "vinus", "venos", "benus", "fenus", "menus", "venous", "venuss", "wenus", "venuz"),
}

# Inizi di comando: permettono di separare la wake word "incollata" al comando ("Venusmetti in pausa")
_GLUED_COMMAND_START = (
    r"(?:metti|mettere|riproduci|suona|pausa|salta|skip|stop|ferma|riprendi|alza|abbassa|volume|basta"
    r"|avanti|prossima|ripeti|mescola|esci|cerca|play|che|chi|come|quanto|dimmi|raccontami)"
)

DEFAULT_KEYWORDS_CONFIG = {
    "primary": "venus",
    "aliases": ["venere"],
    "fuzzy_threshold": 0.78,
}

# Cache in memoria condivisa (invalidata quando il file cambia): la wake word viene controllata
# su OGNI frase pronunciata in vocale, quindi evitiamo di rileggere il JSON dal disco ogni volta.
_CONFIG_LOCK = threading.Lock()
_CONFIG_CACHE: Dict[str, object] = {"key": None, "config": None}


class BotKeywords:
    """
    Gestisce la parola di attivazione (wake word) principale e gli eventuali alias,
    con supporto a:
    - Impostazione rapida della parola di attivazione principale tramite comando admin
    - Alias aggiuntivi opzionali
    - Tolleranza fonetica e fuzzy matching per il riconoscimento vocale in italiano
    """

    def __init__(self):
        root_dir = Path(__file__).resolve().parent
        self.bot_keywords_file = root_dir / "bot_keywords.json"
        if not self.bot_keywords_file.exists():
            self.bot_keywords_file.write_text(
                json.dumps(DEFAULT_KEYWORDS_CONFIG, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _normalize_schema(self, raw_data) -> Dict:
        """Converte il vecchio formato lista ['venus'] nel nuovo schema strutturato."""
        if isinstance(raw_data, list):
            cleaned = [str(k).strip().lower() for k in raw_data if str(k).strip()]
            primary = cleaned[0] if cleaned else "venus"
            aliases = [k for k in cleaned[1:] if k != primary]
            return {
                "primary": primary,
                "aliases": aliases,
                "fuzzy_threshold": 0.78,
            }
        elif isinstance(raw_data, dict):
            primary = str(raw_data.get("primary") or "venus").strip().lower()
            aliases_raw = raw_data.get("aliases", [])
            aliases = []
            for a in aliases_raw:
                a_clean = str(a).strip().lower()
                if a_clean and a_clean != primary and a_clean not in aliases:
                    aliases.append(a_clean)
            threshold = float(raw_data.get("fuzzy_threshold", 0.78))
            return {
                "primary": primary,
                "aliases": aliases,
                "fuzzy_threshold": max(0.60, min(0.95, threshold)),
            }
        return {
            "primary": "venus",
            "aliases": ["venere"],
            "fuzzy_threshold": 0.78,
        }

    @staticmethod
    def _copy_config(config: Dict) -> Dict:
        return {**config, "aliases": list(config["aliases"])}

    def load_config_sync(self) -> Dict:
        """Legge la configurazione dalla cache in memoria (riletta dal disco solo se il file è cambiato)."""
        try:
            st = os.stat(self.bot_keywords_file)
            file_key = (st.st_mtime_ns, st.st_size)
        except OSError:
            file_key = None

        with _CONFIG_LOCK:
            cached = _CONFIG_CACHE["config"]
            if file_key is not None and cached is not None and _CONFIG_CACHE["key"] == file_key:
                return self._copy_config(cached)

        try:
            content = self.bot_keywords_file.read_text(encoding="utf-8")
            raw = json.loads(content) if content.strip() else {}
        except Exception as e:
            logging.warning(f"Errore lettura bot_keywords.json, ripristino default: {e}")
            raw = {}
        config = self._normalize_schema(raw)

        with _CONFIG_LOCK:
            _CONFIG_CACHE["key"] = file_key
            _CONFIG_CACHE["config"] = self._copy_config(config)
        return config

    async def loadConfig(self) -> Dict:
        return self.load_config_sync()

    async def saveConfig(self, config: Dict):
        normalized = self._normalize_schema(config)
        async with aiofiles.open(self.bot_keywords_file, "w", encoding="utf-8") as f:
            await f.write(json.dumps(normalized, indent=2, ensure_ascii=False))
        with _CONFIG_LOCK:
            _CONFIG_CACHE["key"] = None
            _CONFIG_CACHE["config"] = None

    async def getPrimaryKeyword(self) -> str:
        config = await self.loadConfig()
        return config["primary"]

    async def setPrimaryKeyword(self, keyword: str, keep_old_as_alias: bool = False) -> str:
        """Imposta la parola di attivazione principale."""
        clean_kw = keyword.strip().lower()
        clean_kw = re.sub(r"[^\w\sàèéìòù]", "", clean_kw).strip()
        if not clean_kw:
            raise ValueError("La parola di attivazione non può essere vuota.")

        config = await self.loadConfig()
        old_primary = config["primary"]
        config["primary"] = clean_kw

        if clean_kw in config["aliases"]:
            config["aliases"].remove(clean_kw)

        if keep_old_as_alias and old_primary and old_primary != clean_kw:
            if old_primary not in config["aliases"]:
                config["aliases"].insert(0, old_primary)

        await self.saveConfig(config)
        return clean_kw

    async def getAliases(self) -> List[str]:
        config = await self.loadConfig()
        return config["aliases"]

    async def loadBotKeywords(self) -> List[str]:
        """Restituisce tutte le parole di attivazione attive: [primary, *aliases]."""
        config = await self.loadConfig()
        return [config["primary"]] + config["aliases"]

    async def saveBotKeywords(self, keywords: List[str]):
        config = self._normalize_schema(keywords)
        await self.saveConfig(config)

    async def addBotKeyword(self, keyword: str) -> str:
        clean_kw = keyword.strip().lower()
        clean_kw = re.sub(r"[^\w\sàèéìòù]", "", clean_kw).strip()
        if not clean_kw:
            raise ValueError("La parola non può essere vuota.")

        config = await self.loadConfig()
        if clean_kw != config["primary"] and clean_kw not in config["aliases"]:
            config["aliases"].append(clean_kw)
            await self.saveConfig(config)
        return clean_kw

    async def removeBotKeyword(self, keyword: str) -> bool:
        clean_kw = keyword.strip().lower()
        config = await self.loadConfig()
        if clean_kw in config["aliases"]:
            config["aliases"].remove(clean_kw)
            await self.saveConfig(config)
            return True
        if clean_kw == config["primary"] and config["aliases"]:
            # Promuove il primo alias a parola principale se si rimuove la principale
            config["primary"] = config["aliases"].pop(0)
            await self.saveConfig(config)
            return True
        return False

    async def resetKeywords(self, default_primary: str = "venus") -> Dict:
        config = {
            "primary": default_primary.strip().lower(),
            "aliases": ["venere"] if default_primary.strip().lower() == "venus" else [],
            "fuzzy_threshold": 0.78,
        }
        await self.saveConfig(config)
        return config

    def get_wake_words_sync(self) -> List[str]:
        """Parole di attivazione configurate (principale + alias), usate anche come suggerimento per lo STT."""
        config = self.load_config_sync()
        return [config["primary"]] + config["aliases"]

    @staticmethod
    def _keywords_with_variants(config: Dict) -> List[str]:
        keywords = [config["primary"]] + config["aliases"]
        for kw in list(keywords):
            for variant in WAKE_WORD_VARIANTS.get(kw, ()):
                if variant not in keywords:
                    keywords.append(variant)
        return sorted(keywords, key=len, reverse=True)

    def contains_wake_word_sync(self, text: str) -> bool:
        return self.extract_command_sync(text)[0]

    def extract_command_sync(
        self, text: str, config: Optional[Dict] = None
    ) -> Tuple[bool, Optional[str], str]:
        """
        Analizza il testo trascritto in italiano per verificare se contiene la parola di attivazione.
        Supporta:
        - Punteggiatura dopo la wake word (es. "Venus, che ore sono?")
        - Prefissi colloquiali ("Ehi Venus...", "Ok Venus...", "Ciao Venus...")
        - Wake word multi-parola (es. "ok google", "ehi assistente")
        - Wake word incollata al comando ("Venusmetti in pausa") o spezzata in due ("Ve nus")
        - Accenti e maiuscole indifferenti, tolleranza fonetica e fuzzy matching sulle prime parole

        Restituisce:
            (attivato: bool, wake_word_rilevata: Optional[str], comando_residuo: str)
        """
        if not text or not text.strip():
            return False, None, ""

        # Estrai eventuale [EN_HINT: ...] per conservarlo nel comando residuo
        en_hint_suffix = ""
        en_hint_content = ""
        m_hint = re.search(r"(\s*\[EN_HINT:\s*(.+?)\])$", text.strip(), flags=re.IGNORECASE)
        if m_hint:
            en_hint_suffix = m_hint.group(1)
            en_hint_content = m_hint.group(2).strip()
            cleaned_text = text.strip()[: m_hint.start()].strip()
        else:
            cleaned_text = text.strip()

        cleaned_text = unicodedata.normalize("NFC", cleaned_text).replace("’", "'")
        folded = _fold(cleaned_text)

        if config is None:
            config = self.load_config_sync()
        sorted_keywords = self._keywords_with_variants(config)
        threshold = config.get("fuzzy_threshold", 0.75)

        def _activated(kw: str, end_pos: int) -> Tuple[bool, Optional[str], str]:
            remainder = re.sub(r"^[\s,.:;!?\-]+", "", cleaned_text[end_pos:]).strip()
            return True, kw, f"{remainder}{en_hint_suffix}".strip() if remainder else ""

        # 1. Controllo regex diretto (senza accenti/maiuscole) all'inizio o nelle primissime parole
        for kw in sorted_keywords:
            fkw = re.escape(_fold(kw))
            m = re.match(rf"(?:(?:{_PREFIXES_PATTERN})[\s,.:;!?]+)*({fkw})\b", folded)
            if m:
                return _activated(kw, m.end(1))

            # Controllo anche se la wake word è preceduta da massimo 1-2 parole nella frase
            m2 = re.search(rf"\b({fkw})\b", folded)
            if m2 and m2.start() <= 24:
                return _activated(kw, m2.end(1))

        # 2. Wake word incollata al comando dal riconoscitore vocale ("venusmetti in pausa")
        for kw in sorted_keywords:
            fkw = re.escape(_fold(kw))
            m = re.match(
                rf"(?:(?:{_PREFIXES_PATTERN})[\s,.:;!?]+)*({fkw})(?={_GLUED_COMMAND_START})", folded
            )
            if m:
                return _activated(kw, m.end(1))

        # 3. Controllo fonetico e fuzzy sulle prime parole della frase
        tokens = list(re.finditer(r"\w+", folded))
        if not tokens:
            return False, None, ""

        start_idx = 0
        while (
            start_idx < len(tokens) - 1
            and tokens[start_idx].group(0) in ITALIAN_GREETING_PREFIXES
        ):
            start_idx += 1

        candidate_starts = [start_idx] if start_idx == 0 else [0, start_idx]
        # Il fuzzy usa solo le parole configurate: le varianti ortografiche valgono solo se esatte
        # (altrimenti "menu del giorno" somiglierebbe alla variante "menus")
        configured_keywords = sorted([config["primary"]] + config["aliases"], key=len, reverse=True)

        for s_idx in candidate_starts:
            for kw in configured_keywords:
                kw_words = kw.split()
                num_words = len(kw_words)
                norm_kw = _fold(kw)
                effective_threshold = max(threshold, 0.88) if len(norm_kw) <= 3 else threshold

                spans = []
                if s_idx + num_words <= len(tokens):
                    spans.append((tokens[s_idx : s_idx + num_words], " "))
                # Wake word spezzata in due dal riconoscitore ("ve nus", "vi nus"): solo se le due parole
                # unite sono lunghe almeno quanto la wake word (altrimenti "ve ne" somiglierebbe a "venere")
                if (
                    num_words == 1
                    and s_idx + 2 <= len(tokens)
                    and len(norm_kw) >= 5
                    and len(tokens[s_idx].group(0)) + len(tokens[s_idx + 1].group(0)) >= len(norm_kw)
                ):
                    spans.append((tokens[s_idx : s_idx + 2], ""))

                for span_tokens, joiner in spans:
                    spoken_phrase = joiner.join(t.group(0) for t in span_tokens)
                    ratio_direct = difflib.SequenceMatcher(None, spoken_phrase, norm_kw).ratio()

                    phon_spoken = joiner.join(_italian_phonetic_key(t.group(0)) for t in span_tokens)
                    phon_kw = " ".join(_italian_phonetic_key(w) for w in kw_words)
                    ratio_phonetic = (
                        difflib.SequenceMatcher(None, phon_spoken, phon_kw).ratio()
                        if phon_spoken and phon_kw
                        else 0.0
                    )

                    best_ratio = max(ratio_direct, ratio_phonetic)
                    if best_ratio >= effective_threshold:
                        logging.debug(
                            f"Wake word '{kw}' rilevata tramite match fonetico/fuzzy "
                            f"('{spoken_phrase}', score={best_ratio:.2f})"
                        )
                        return _activated(kw, span_tokens[-1].end())

        # 4. Se la prima ipotesi non conteneva la wake word ma una delle ipotesi alternative in EN_HINT sì, usala!
        # Tra più alternative valide scegli quella più simile alla trascrizione principale italiana
        # (l'ipotesi en-US serve per i titoli, ma il comando italiano va preso dall'ipotesi italiana).
        if en_hint_content:
            best_alt: Optional[Tuple[float, str, str]] = None
            for alt_candidate in en_hint_content.split("|"):
                alt_raw = alt_candidate.strip()
                alt_c = _fold(alt_raw)
                if not alt_c:
                    continue
                for kw in sorted_keywords:
                    m_alt = re.match(
                        rf"(?:(?:{_PREFIXES_PATTERN})[\s,.:;!?]+)*({re.escape(_fold(kw))})\b[\s,.:;!?]*(.*)$",
                        alt_c,
                    )
                    if m_alt:
                        rem = alt_raw[m_alt.start(2):].strip()
                        score = difflib.SequenceMatcher(None, _fold(rem), folded).ratio()
                        if best_alt is None or score > best_alt[0]:
                            best_alt = (score, kw, rem)
                        break
            if best_alt is not None:
                _, kw, rem = best_alt
                return True, kw, f"{rem}{en_hint_suffix}".strip() if rem else ""

        return False, None, ""

    async def extractCommandAfterWakeWord(self, text: str) -> Tuple[bool, Optional[str], str]:
        return self.extract_command_sync(text)
