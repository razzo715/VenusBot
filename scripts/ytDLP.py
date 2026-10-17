import asyncio
import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL


def getSongExpiration(url: str) -> Optional[int]:
    if not url or not str(url).startswith("http"):
        return None
    try:
        parsed = urlparse(url)
        if parsed.query:
            params = parse_qs(parsed.query)
            expire = params.get("expire") or params.get("expires")
            if expire:
                return int(expire[0])

        match = re.search(r"/expire/(\d+)", parsed.path)
        if match:
            return int(match.group(1))
    except Exception:
        return None
    return None


def _clean_title(title: str) -> str:
    if not title:
        return "Brano sconosciuto"
    cleaned = re.sub(r"[^\w\s\-\(\)\[\]&'.,àèéìòùÀÈÉÌÒÙ]", "", title).strip()
    return cleaned or title.strip()


# Parole tipiche di video di YouTuber / Gameplay / Vlog da escludere quando si cerca musica
NON_MUSIC_BLACKLIST = (
    "gameplay",
    "walkthrough",
    "let's play",
    "lets play",
    "vlog",
    "reaction",
    "reazione",
    "episodio",
    "minecraft",
    "fortnite",
    "roblox",
    "gta",
    "brawl stars",
    "clash royale",
    "recensione",
    "tutorial",
    "unboxing",
    "intervista",
    "podcast",
    "telecronaca",
    "highlights",
    "challenge",
    "prank",
    "scherzo",
)


def _score_music_candidate(
    entry: dict, original_query: str, expected_duration: Optional[int] = None
) -> int:
    """
    Calcola un punteggio per distinguere un vero brano musicale da un video di YouTuber.
    Restituisce un punteggio negativo se il video non è musicale.
    """
    title = (entry.get("title") or "").lower()
    channel = (entry.get("channel") or entry.get("uploader") or "").lower()
    duration = int(entry.get("duration") or 0)
    query_lower = original_query.lower()

    # Escludi video troppo corti (< 35s, es. Shorts/Preview) o troppo lunghi (> 12 min, es. Vlog/Gameplay)
    if duration > 0 and (duration < 35 or duration > 720):
        return -100

    # Escludi parole da YouTuber/Gameplay a meno che non siano esplicitamente nella query
    for bad_word in NON_MUSIC_BLACKLIST:
        if bad_word in title and bad_word not in query_lower:
            return -100

    score = 0
    # Se conosciamo la durata esatta del brano da Spotify, premia i risultati con durata quasi identica
    if expected_duration and expected_duration > 0 and duration > 0:
        diff = abs(duration - expected_duration)
        if diff <= 8:
            score += 45
        elif diff <= 20:
            score += 25
        elif diff > 90:
            score -= 40

    # Canali ufficiali di YouTube Music ("Artista - Topic") o VEVO hanno priorità assoluta
    if "- topic" in channel or "vevo" in channel:
        score += 50
    if "official" in channel or "records" in channel or "music" in channel:
        score += 20

    # Titoli tipici di tracce musicali
    if "official audio" in title or "audio ufficiale" in title:
        score += 35
    elif "lyrics" in title or "testo" in title or "lyric video" in title:
        score += 25
    elif "official video" in title or "video ufficiale" in title or "official music video" in title:
        score += 20

    # Durata tipica di una canzone (1m30s - 6m00s)
    if 90 <= duration <= 360:
        score += 15

    return score


class VideoSearcher:
    def __init__(self):
        root_dir = Path(__file__).resolve().parent.parent
        self.cookies_path = root_dir / "cookies.txt"
        self.cache_dir = Path(tempfile.gettempdir()) / "venusbot_music"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ytdlp_cache_dir = Path(tempfile.gettempdir()) / "venusbot_ytdlp_cache"

    def _get_base_options(self) -> dict:
        """
        Opzioni base yt-dlp:
        - Usa i client predefiniti di yt-dlp 2026 ('tv', 'android_sdkless', 'web')
          evitando 'android_vr' e 'ios' che causano HTTP Error 403 Forbidden.
        - Forza IPv4 ('source_address': '0.0.0.0') per evitare Read Timeout su Docker.
        - Cache su disco del player YouTube già elaborato: senza, ogni brano riscaricava e
          rielaborava il player JavaScript (secondi di attesa in più a ogni richiesta).
        - Abilita deno e node (>= 22) per risolvere le challenge JavaScript di YouTube (yt-dlp-ejs).
        """
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "cachedir": str(self.ytdlp_cache_dir),
            "js_runtimes": {"deno": {}, "node": {}},
            "nocheckcertificate": True,
            "geo_bypass": True,
            "source_address": "0.0.0.0",
            "socket_timeout": 10,
            "retries": 2,
            "fragment_retries": 2,
        }
        if self.cookies_path.exists() and self.cookies_path.stat().st_size > 32:
            opts["cookiefile"] = str(self.cookies_path)
        return opts

    def _select_best_music_candidate(
        self, query: str, expected_duration: Optional[int] = None
    ) -> Tuple[str, str]:
        """
        Cerca su YouTube filtrando esclusivamente brani musicali (Topic, VEVO, Official Audio)
        ed escludendo video di YouTuber, gameplay o vlog.
        Restituisce (target_url, clean_title_or_query).
        """
        search_query = query
        if not any(
            w in query.lower()
            for w in ("official", "audio", "lyrics", "song", "canzone", "brano")
        ):
            search_query = f"{query} official audio"

        flat_opts = {
            **self._get_base_options(),
            "noplaylist": True,
            "extract_flat": "in_playlist",
            "default_search": "ytsearch6",
            "ignoreerrors": True,
        }
        try:
            with YoutubeDL(flat_opts) as ytdlp:
                info = ytdlp.extract_info(search_query, download=False)
                entries = [e for e in (info.get("entries") or []) if e] if info else []
                if entries:
                    scored = sorted(
                        entries,
                        key=lambda e: _score_music_candidate(e, query, expected_duration),
                        reverse=True,
                    )
                    best = scored[0]
                    raw_url = best.get("url") or best.get("webpage_url") or best.get("id")
                    best_title = _clean_title(best.get("title") or query)
                    if raw_url:
                        if not str(raw_url).startswith("http"):
                            raw_url = f"https://www.youtube.com/watch?v={raw_url}"
                        logging.info(
                            f"[Filtro Musica] Scelto brano: '{best_title}' "
                            f"(canale: {best.get('channel') or best.get('uploader')})"
                        )
                        return raw_url, best_title
        except Exception as e:
            logging.debug(f"Ricerca selettiva musicale fallback: {e}")

        return f"ytsearch1:{search_query}", query

    def _try_download_single(
        self, target: str, extra_opts: Optional[dict] = None
    ) -> Optional[dict]:
        """
        Tenta l'estrazione/download da un target (URL o ricerca) con le opzioni specificate.
        Se il video è una live stream (es. radio /lofi), non scarica il file ma restituisce lo stream HLS.
        """
        file_token = uuid.uuid4().hex[:12]
        outtmpl = str(self.cache_dir / f"track_{file_token}.%(ext)s")

        opts = {
            **self._get_base_options(),
            "noplaylist": True,
            "outtmpl": outtmpl,
        }
        if extra_opts:
            opts.update(extra_opts)

        with YoutubeDL(opts) as ytdlp:
            raw_info = ytdlp.extract_info(target, download=False, process=False)
            if not raw_info:
                return None

            duration = int(raw_info.get("duration") or 0)
            is_live = bool(raw_info.get("is_live")) or raw_info.get("live_status") == "is_live"

            # Se è una radio live 24/7 (is_live o > 25 min), estrai solo lo stream senza scaricare file
            if is_live or duration > 1500:
                info = ytdlp.process_ie_result(raw_info, download=False)
                if not info:
                    return None
                video = (
                    [e for e in (info.get("entries") or []) if e][0]
                    if "entries" in info
                    else info
                )
                stream_url = video.get("url")
                if not stream_url:
                    return None
                return {
                    "title": _clean_title(video.get("title", "")),
                    "duration": 0 if is_live else int(video.get("duration") or duration),
                    "thumbnail": video.get("thumbnail"),
                    "link": stream_url,
                    "url": video.get("webpage_url") or target,
                    "http_headers": video.get("http_headers") or {},
                    "is_local_cache": False,
                }

            # Altrimenti processa e scarica in un UNICO passaggio (0 doppie richieste a YouTube!)
            dl_info = ytdlp.process_ie_result(raw_info, download=True)
            if not dl_info:
                return None
            if "entries" in dl_info:
                entries = [e for e in (dl_info.get("entries") or []) if e]
                if not entries:
                    return None
                dl_info = entries[0]

            downloaded_path = ytdlp.prepare_filename(dl_info)
            if (
                downloaded_path
                and Path(downloaded_path).exists()
                and Path(downloaded_path).stat().st_size > 1024
            ):
                return {
                    "title": _clean_title(dl_info.get("title", "")),
                    "duration": int(dl_info.get("duration") or duration),
                    "thumbnail": dl_info.get("thumbnail"),
                    "link": str(downloaded_path),
                    "url": dl_info.get("webpage_url") or target,
                    "http_headers": dl_info.get("http_headers") or {},
                    "is_local_cache": True,
                }
        return None

    def _download_from_soundcloud(self, search_query: str) -> Optional[dict]:
        """
        Fallback automatico su SoundCloud quando YouTube restituisce HTTP 403 Forbidden sull'IP del server.
        Filtra automaticamente le anteprime di 30 secondi scegliendo solo brani completi.
        """
        clean_q = re.sub(
            r"\b(official\s+audio|official\s+video|lyrics|lyric\s+video|audio\s+ufficiale)\b",
            "",
            search_query,
            flags=re.IGNORECASE,
        ).strip()
        if not clean_q:
            clean_q = search_query

        logging.info(f"[Fallback Anti-403] Ricerca del brano su SoundCloud: '{clean_q}'")

        file_token = uuid.uuid4().hex[:12]
        outtmpl = str(self.cache_dir / f"track_sc_{file_token}.%(ext)s")

        sc_opts = {
            **self._get_base_options(),
            "noplaylist": True,
            "default_search": "scsearch6",
            "ignoreerrors": True,
            "outtmpl": outtmpl,
        }
        try:
            with YoutubeDL(sc_opts) as ytdlp:
                info = ytdlp.extract_info(f"scsearch6:{clean_q}", download=False)
                entries = [e for e in (info.get("entries") or []) if e] if info else []
                if not entries:
                    return None

                # Scegli il primo brano completo (durata >= 45s per evitare le preview Go+ da 30s)
                chosen = None
                for entry in entries:
                    dur = int(entry.get("duration") or 0)
                    if 45 <= dur <= 720:
                        chosen = entry
                        break
                if not chosen:
                    chosen = entries[0]

                sc_url = chosen.get("webpage_url") or chosen.get("url")
                if not sc_url:
                    return None

                dl_info = ytdlp.extract_info(sc_url, download=True)
                if dl_info and "entries" in dl_info:
                    dl_info = [e for e in dl_info["entries"] if e][0]
                downloaded_path = ytdlp.prepare_filename(dl_info)
                if (
                    downloaded_path
                    and Path(downloaded_path).exists()
                    and Path(downloaded_path).stat().st_size > 1024
                ):
                    title = _clean_title(chosen.get("title") or clean_q)
                    logging.info(f"[Fallback SoundCloud] Scaricato con successo: '{title}'")
                    return {
                        "title": title,
                        "duration": int(chosen.get("duration") or 0),
                        "thumbnail": chosen.get("thumbnail"),
                        "link": str(downloaded_path),
                        "url": sc_url,
                        "http_headers": chosen.get("http_headers") or {},
                        "is_local_cache": True,
                    }
        except Exception as e:
            logging.warning(f"Errore fallback SoundCloud per '{clean_q}': {e}")

        return None

    def _download_or_extract(
        self,
        target: str,
        is_search: bool = False,
        expected_duration: Optional[int] = None,
    ) -> Optional[dict]:
        """
        Pipeline di estrazione audio a 3 livelli (100% anti-403):
        1. YouTube con client predefiniti yt-dlp 2026 ('tv', 'android_sdkless', 'web')
        2. YouTube con client HLS Safari ('web_safari', 'tv_downgraded') che non richiede GVS PO Token
        3. Fallback automatico su SoundCloud ('scsearch') se YouTube blocca l'IP con 403
        """
        fallback_query = target
        if is_search:
            resolved_target, fallback_query = self._select_best_music_candidate(
                target, expected_duration=expected_duration
            )
        else:
            resolved_target = target

        # Se è già un link SoundCloud, scaricalo direttamente
        if "soundcloud.com" in resolved_target.lower():
            try:
                res = self._try_download_single(resolved_target)
                if res:
                    return res
            except Exception as e:
                logging.error(f"Errore download SoundCloud: {e}")
                return None

        # Livello 1: YouTube Client Predefiniti ('tv', 'android_sdkless', 'web')
        try:
            res = self._try_download_single(resolved_target)
            if res:
                return res
        except Exception as e1:
            logging.warning(f"[YouTube Livello 1] Fallito ({e1}), provo client HLS web_safari...")

        # Livello 2: YouTube Client HLS ('web_safari', 'tv_downgraded')
        try:
            safari_opts = {
                "format": "bestaudio/best",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web_safari", "tv_downgraded", "android_sdkless"],
                    }
                },
            }
            res = self._try_download_single(resolved_target, extra_opts=safari_opts)
            if res:
                return res
        except Exception as e2:
            logging.warning(f"[YouTube Livello 2] Fallito ({e2}), attivo fallback SoundCloud...")

        # Livello 3: Fallback automatico su SoundCloud
        if not is_search and resolved_target.startswith("http"):
            # Se l'input era un URL YouTube diretto, prova a recuperarne il titolo per cercarlo su SoundCloud
            try:
                with YoutubeDL({**self._get_base_options(), "extract_flat": True}) as ytdlp:
                    meta = ytdlp.extract_info(resolved_target, download=False)
                    if meta and meta.get("title"):
                        fallback_query = meta["title"]
            except Exception:
                pass

        return self._download_from_soundcloud(fallback_query)

    async def getVideoInfoFromURL(self, video_url: str):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_or_extract, video_url, False, None)

    async def getVideoInfoFromQuery(
        self, video_query: str, expected_duration: Optional[int] = None
    ):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._download_or_extract, video_query, True, expected_duration
        )

    async def getRelatedMusicTrack(
        self, seed_title: str, exclude_titles: Optional[list[str]] = None
    ) -> Optional[str]:
        """
        Cerca su YouTube un brano musicale correlato (stesso artista o mix simile)
        escludendo i brani già ascoltati.
        """
        loop = asyncio.get_running_loop()

        def _find_related():
            exclude_words = set()
            for t in (exclude_titles or []) + [seed_title]:
                if not t:
                    continue
                tl = t.lower().strip()
                exclude_words.add(tl)
                if "-" in tl:
                    exclude_words.add(tl.split("-", 1)[1].strip())

            artist_part = (
                seed_title.split("-", 1)[0].strip()
                if "-" in seed_title
                else seed_title.strip()
            )
            flat_opts = {
                **self._get_base_options(),
                "noplaylist": True,
                "extract_flat": "in_playlist",
                "default_search": "ytsearch10",
                "ignoreerrors": True,
            }
            try:
                with YoutubeDL(flat_opts) as ytdlp:
                    info = ytdlp.extract_info(
                        f"{artist_part} top songs official audio", download=False
                    )
                    entries = [e for e in (info.get("entries") or []) if e] if info else []
                    for entry in entries:
                        if _score_music_candidate(entry, artist_part) < 0:
                            continue
                        cand_title = _clean_title(entry.get("title") or "")
                        cand_lower = cand_title.lower()
                        if any(ex and len(ex) > 3 and ex in cand_lower for ex in exclude_words):
                            continue
                        return cand_title
            except Exception as e:
                logging.debug(f"Errore getRelatedMusicTrack YouTube: {e}")
            return None

        return await loop.run_in_executor(None, _find_related)

    async def getSearchResults(self, video_query: str):
        loop = asyncio.get_running_loop()

        def extract_info():
            yt_dlp_options = {
                **self._get_base_options(),
                "noplaylist": True,
                "default_search": "ytsearch10",
                "ignoreerrors": True,
                "extract_flat": "in_playlist",
            }
            with YoutubeDL(yt_dlp_options) as ytdlp:
                info = ytdlp.extract_info(f"{video_query} official audio", download=False)
                if not info:
                    return []
                entries = info.get("entries") if "entries" in info else [info]
                results = []
                for entry in entries or []:
                    if not entry:
                        continue
                    if _score_music_candidate(entry, video_query) < 0:
                        continue
                    raw_link = entry.get("url") or entry.get("webpage_url") or entry.get("id")
                    if not raw_link:
                        continue
                    if not str(raw_link).startswith("http"):
                        raw_link = f"https://www.youtube.com/watch?v={raw_link}"
                    results.append(
                        {
                            "title": _clean_title(entry.get("title", "")),
                            "link": raw_link,
                        }
                    )
                return results

        return await loop.run_in_executor(None, extract_info)

    async def getPlaylistInfo(self, playlist_url: str):
        loop = asyncio.get_running_loop()

        def extract_info():
            yt_dlp_options = {
                **self._get_base_options(),
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
            }
            with YoutubeDL(yt_dlp_options) as ytdlp:
                playlist = ytdlp.extract_info(playlist_url, download=False)
                if not playlist:
                    return None
                entries = [e for e in (playlist.get("entries") or []) if e]
                metadata = {
                    "playlist_name": _clean_title(playlist.get("title", "Playlist")),
                    "song_count": len(entries),
                    "thumbnail": playlist.get("thumbnail")
                    or (
                        playlist.get("thumbnails", [{}])[0].get("url")
                        if playlist.get("thumbnails")
                        else None
                    ),
                }
                song_urls = []
                for entry in entries:
                    u = entry.get("url") or entry.get("webpage_url") or entry.get("id")
                    if u:
                        if not str(u).startswith("http"):
                            u = f"https://www.youtube.com/watch?v={u}"
                        song_urls.append({"url": u})
                return [metadata] + song_urls

        return await loop.run_in_executor(None, extract_info)
