import os
import aiohttp
import re
from dotenv import load_dotenv
from pathlib import Path
import logging
from typing import Optional


class SpotifyController:
    def __init__(self):
        load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")
        self.__client_id = (os.getenv("SPOTIFY_CLIENT_ID") or "").strip().strip("'\"")
        self.__client_secret = (os.getenv("SPOTIFY_CLIENT_SECRET") or "").strip().strip("'\"")
        self.__refresh_token = (os.getenv("SPOTIFY_CLIENT_REFRESH_TOKEN") or "").strip().strip("'\"")
        self.__token_url = "https://accounts.spotify.com/api/token"
        self.__access_token: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(keepalive_timeout=60))
        return self._session

    def is_configured(self) -> bool:
        return bool(self.__client_id and self.__client_secret)

    async def refresh_token(self) -> Optional[str]:
        if not self.__client_id or not self.__client_secret:
            return None

        async with aiohttp.ClientSession() as session:
            # 1. Prova prima con refresh_token se presente
            if self.__refresh_token:
                payload_rt = {
                    "grant_type": "refresh_token",
                    "refresh_token": self.__refresh_token,
                    "client_id": self.__client_id,
                    "client_secret": self.__client_secret,
                }
                try:
                    async with session.post(
                        self.__token_url,
                        data=payload_rt,
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            self.__access_token = data.get("access_token")
                            if self.__access_token:
                                logging.debug("Token Spotify aggiornato (refresh_token).")
                                return self.__access_token
                except Exception as e:
                    logging.debug(f"Errore refresh_token Spotify: {e}")

            # 2. Fallback standard Client Credentials (funziona sempre con CLIENT_ID + CLIENT_SECRET)
            payload_cc = {
                "grant_type": "client_credentials",
                "client_id": self.__client_id,
                "client_secret": self.__client_secret,
            }
            try:
                async with session.post(
                    self.__token_url,
                    data=payload_cc,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        self.__access_token = data.get("access_token")
                        if self.__access_token:
                            logging.debug("Token Spotify ottenuto (client_credentials).")
                            return self.__access_token
                    else:
                        text = await response.text()
                        logging.warning(f"Autenticazione Spotify fallita ({response.status}): {text}")
            except Exception as e:
                logging.debug(f"Errore client_credentials Spotify: {e}")

        return None

    async def get_access_token(self) -> Optional[str]:
        if self.__access_token is None:
            return await self.refresh_token()
        return self.__access_token

    async def searchTrack(self, query: str) -> Optional[dict]:
        """
        Cerca il brano direttamente nel catalogo ufficiale di Spotify (esclusivamente musica!).
        Restituisce titolo ufficiale, artista, copertina album, durata esatta e link Spotify.
        """
        if not self.is_configured() or not query or not query.strip():
            return None

        clean_query = re.sub(
            r"\b(official\s+audio|official\s+video|lyrics|audio\s+ufficiale)\b",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip()
        if not clean_query:
            clean_query = query.strip()

        access_token = await self.get_access_token()
        if not access_token:
            return None

        search_url = "https://api.spotify.com/v1/search"
        params = {
            "q": clean_query,
            "type": "track",
            "market": "IT",
            "limit": "3",
        }

        # Sessione HTTP persistente: niente nuovo handshake TLS a ogni brano richiesto a voce
        session = await self._get_session()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {access_token}"}
            try:
                async with session.get(
                    search_url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("tracks", {}).get("items", [])
                        if not items:
                            return None
                        track = items[0]
                        title = track.get("name", "")
                        artists = [a["name"] for a in track.get("artists", []) if a.get("name")]
                        artist_str = ", ".join(artists[:2]) if artists else ""
                        primary_artist = artists[0] if artists else ""
                        images = track.get("album", {}).get("images", [])
                        thumbnail = images[0]["url"] if images else None
                        duration_sec = int(round((track.get("duration_ms") or 0) / 1000))
                        spotify_url = track.get("external_urls", {}).get("spotify")

                        full_display = f"{artist_str} - {title}" if artist_str else title
                        search_query = f"{primary_artist} - {title}" if primary_artist else title

                        logging.info(
                            f"[Catalogo Spotify] Trovato brano ufficiale: '{full_display}' ({duration_sec}s)"
                        )
                        return {
                            "title": title,
                            "artist": primary_artist,
                            "full_title": full_display,
                            "search_query": search_query,
                            "thumbnail": thumbnail,
                            "duration": duration_sec,
                            "spotify_url": spotify_url,
                        }
                    elif resp.status == 401 and attempt == 0:
                        access_token = await self.refresh_token()
                        if not access_token:
                            break
                    else:
                        break
            except Exception as e:
                logging.debug(f"Errore ricerca catalogo Spotify: {e}")
                break

        return None

    async def getRelatedTrack(
        self, seed_title: str, exclude_titles: Optional[list[str]] = None
    ) -> Optional[dict]:
        """
        Trova un brano correlato/dello stesso artista su Spotify per la modalità Autoplay.
        """
        if not self.is_configured() or not seed_title:
            return None

        # Estrai solo i titoli puri e i titoli completi già riprodotti da escludere
        exclude_full = set()
        exclude_short = set()
        for t in (exclude_titles or []) + [seed_title]:
            if not t:
                continue
            tl = t.lower().strip()
            exclude_full.add(tl)
            if "-" in tl:
                exclude_short.add(tl.split("-", 1)[1].strip())
            else:
                exclude_short.add(tl)

        # Determina l'artista del brano seme
        artist_part = ""
        if "-" in seed_title:
            artist_part = seed_title.split("-", 1)[0].strip()
        else:
            seed_info = await self.searchTrack(seed_title)
            if seed_info and seed_info.get("artist"):
                artist_part = seed_info["artist"]
                if seed_info.get("title"):
                    exclude_short.add(seed_info["title"].lower().strip())

        if not artist_part:
            artist_part = seed_title.strip()

        access_token = await self.get_access_token()
        if not access_token:
            return None

        search_url = "https://api.spotify.com/v1/search"
        params = {
            "q": artist_part,
            "type": "track",
            "market": "IT",
            "limit": "10",
        }

        async with aiohttp.ClientSession() as session:
            for attempt in range(2):
                headers = {"Authorization": f"Bearer {access_token}"}
                try:
                    async with session.get(
                        search_url,
                        headers=headers,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=6),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            items = data.get("tracks", {}).get("items", [])
                            for track in items:
                                title = (track.get("name") or "").strip()
                                if not title:
                                    continue
                                title_clean = re.sub(r"\s*\(.*?\)", "", title).lower().strip()
                                artists = [a["name"] for a in track.get("artists", []) if a.get("name")]
                                primary_artist = artists[0] if artists else ""
                                full_display = f"{primary_artist} - {title}" if primary_artist else title
                                if (
                                    full_display.lower() in exclude_full
                                    or title.lower() in exclude_short
                                    or title_clean in exclude_short
                                ):
                                    continue
                                images = track.get("album", {}).get("images", [])
                                return {
                                    "title": title,
                                    "artist": primary_artist,
                                    "full_title": full_display,
                                    "search_query": full_display,
                                    "thumbnail": images[0]["url"] if images else None,
                                    "duration": int(round((track.get("duration_ms") or 0) / 1000)),
                                }
                            break
                        elif resp.status == 401 and attempt == 0:
                            access_token = await self.refresh_token()
                            if not access_token:
                                break
                        else:
                            break
                except Exception as e:
                    logging.debug(f"Errore Autoplay Spotify: {e}")
                    break

        return None

    def extract_track_id(self, spotify_url: str) -> str:
        match = re.search(r"spotify\.com/(?:intl-[a-z]+/)?track/([a-zA-Z0-9]+)", spotify_url)
        if match:
            return match.group(1)
        raise ValueError("URL traccia Spotify non valido")

    async def _fallback_oembed_track(self, spotify_url: str) -> dict | None:
        """Fallback gratuito senza chiavi API tramite Spotify oEmbed."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://open.spotify.com/oembed",
                    params={"url": spotify_url},
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("title")
                        thumbnail = data.get("thumbnail_url")
                        if title:
                            return {"title": title, "artist": "", "thumbnail": thumbnail}
        except Exception as e:
            logging.debug(f"Fallback oEmbed Spotify fallito: {e}")
        return None

    async def getSpotifySongInfo(self, spotify_url: str) -> dict:
        track_id = self.extract_track_id(spotify_url)
        api_url = f"https://api.spotify.com/v1/tracks/{track_id}"

        access_token = await self.get_access_token()
        if access_token:
            async with aiohttp.ClientSession() as session:
                for attempt in range(2):
                    headers = {"Authorization": f"Bearer {access_token}"}
                    async with session.get(api_url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            images = data.get("album", {}).get("images", [])
                            return {
                                "title": data["name"],
                                "artist": data["artists"][0]["name"] if data.get("artists") else "",
                                "thumbnail": images[0]["url"] if images else None,
                                "duration": int(round((data.get("duration_ms") or 0) / 1000)),
                            }
                        elif resp.status == 401 and attempt == 0:
                            access_token = await self.refresh_token()
                            if not access_token:
                                break

        # Fallback oEmbed senza credenziali
        oembed_res = await self._fallback_oembed_track(spotify_url)
        if oembed_res:
            return oembed_res

        raise Exception("Impossibile recuperare informazioni sul brano Spotify.")

    async def getSpotifyPlaylistInfo(self, spotify_url: str) -> list:
        playlist_match = re.search(
            r"spotify\.com/(?:intl-[a-z]+/)?playlist/([a-zA-Z0-9]+)", spotify_url
        )
        album_match = re.search(
            r"spotify\.com/(?:intl-[a-z]+/)?album/([a-zA-Z0-9]+)", spotify_url
        )

        if playlist_match:
            endpoint = f"https://api.spotify.com/v1/playlists/{playlist_match.group(1)}"
        elif album_match:
            endpoint = f"https://api.spotify.com/v1/albums/{album_match.group(1)}"
        else:
            raise ValueError("URL playlist o album Spotify non valido")

        access_token = await self.get_access_token()
        if not access_token:
            raise Exception(
                "Credenziali Spotify non configurate nel file .env per caricare intere playlist."
            )

        async with aiohttp.ClientSession() as session:
            for attempt in range(2):
                headers = {"Authorization": f"Bearer {access_token}"}
                async with session.get(endpoint, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = data.get("name", "Playlist Spotify")
                        thumbnail = data["images"][0]["url"] if data.get("images") else None
                        items = data.get("tracks", {}).get("items", [])
                        track_list = [
                            {
                                "title": (
                                    item["track"]["name"]
                                    if "playlist" in endpoint
                                    else item["name"]
                                ),
                                "artist": (
                                    item["track"]["artists"][0]["name"]
                                    if "playlist" in endpoint
                                    else item["artists"][0]["name"]
                                ),
                            }
                            for item in items
                            if (item.get("track") if "playlist" in endpoint else item.get("name"))
                        ]
                        return [{"title": title, "thumbnail": thumbnail}] + track_list
                    elif resp.status == 401 and attempt == 0:
                        access_token = await self.refresh_token()
                        if not access_token:
                            break

            raise Exception("Impossibile recuperare la playlist/album da Spotify.")
