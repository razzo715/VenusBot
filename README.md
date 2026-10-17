# 🎙️🎶 Venus Bot — Assistente Vocale Intelligente (Stile Google Assistant) & Music Bot 24/7 per Discord

**Venus Bot** è un bot Discord 24/7 completamente ricostruito e ottimizzato per la **lingua italiana**, che unisce un **riproduttore musicale avanzato** (YouTube, Spotify, SoundCloud, File Audio, Radio Live) a un **vero Assistente Vocale Intelligente in stile Google Assistant / Alexa**.

Parla direttamente nel canale vocale pronunciando la **parola di attivazione** (es. *"Venus"* o qualsiasi parola impostata dagli Admin con `/setwakeword`): il bot ti ascolta in italiano, esegue comandi musicali o **risponde a voce nel canale vocale** a qualsiasi domanda usando una voce neurale italiana naturale!

---

## ✨ Caratteristiche Principali

- 🗣️ **Riconoscimento Vocale in Italiano ad Alta Precisione**:
  - Pipeline audio a 48kHz stereo $\rightarrow$ mono anti-clipping ottimizzata per Discord.
  - Doppio motore Cloud STT gratuito: **Groq Whisper Large v3 Turbo (`it`)** + **Google Speech Recognition (`it-IT`)** automatico senza chiavi.
  - Tolleranza fonetica e *fuzzy matching* per riconoscere la parola di attivazione anche con accenti, punteggiatura o lievi imprecisioni di pronuncia (es. *"Ehi Venus, ..."*, *"Ok Venus, ..."*), anche quando il riconoscitore la incolla al comando (*"Venusmetti..."*) o la spezza (*"Ve nus"*).
  - **Trascrizione anticipata**: appena smetti di parlare la trascrizione parte già durante la pausa, così il testo è pronto nel momento in cui la frase si chiude (~0,4-0,5 s in meno a ogni comando).
  - Motori STT in cascata con *hedging*: se il motore principale è lento parte in parallelo anche il successivo; connessioni HTTP keep-alive (niente handshake TLS a ogni frase).
- 🧩 **Comprensione dei Comandi in Italiano Naturale (100% locale, < 1 ms)**:
  - Ignora punteggiatura, accenti, intercalari (*"allora"*, *"senti"*, *"puoi"*) e cortesie (*"per favore"*, *"grazie"*).
  - Capisce varianti come *"metti pausa"*, *"ferma un attimo"*, *"passa alla prossima"*, *"canzone successiva"*, *"mettimi..."*, *"voglio sentire..."*, *"niente, lascia stare"*.
  - Volume con qualsiasi numero, anche a parole: *"volume trentadue"*, *"abbassa al 20 per cento"*, *"alza al massimo"*.
- 🧠 **Intelligenza Artificiale Stile Google Assistant (Solo Voce in Canale Vocale)**:
  - Risponde a domande di cultura generale, curiosità, calcoli, barzellette, ora/data o consigli musicali **parlando direttamente nel canale vocale**.
  - **Sospensione Intelligente della Musica (`InterruptibleOpusSource`)**: se una canzone sta suonando e fai una domanda all'assistente, la musica va in pausa istantaneamente mentre il bot risponde a voce e **riprende dal millisecondo esatto** appena finisce di parlare!
  - Supporta i migliori provider Cloud AI gratuiti: **Groq Cloud (Llama 3.3 70B)**, **Google Gemini (Gemini 2.0 Flash)** e un **fallback Cloud AI gratuito zero-config** che funziona anche senza chiavi API.
- 🎯 **Parola di Attivazione (Wake Word) Configurabile da Admin**:
  - Comando rapido `/setwakeword <parola>` per cambiare subito la parola di attivazione principale.
  - Comando completo `/wakeword` (`imposta`, `aggiungi_alias`, `rimuovi_alias`, `lista`, `reset`) e pannello interattivo `/keywords`.
- 🎧 **Musica 24/7 Completa**:
  - Riproduzione da **YouTube**, **Spotify** (anche senza API key per i brani singoli tramite fallback oEmbed), **SoundCloud** e file caricati.
  - Gestione coda interattiva (`/queue`), ricerca top 10 (`/search`), controllo volume (`/volume` o a voce), radio 24/7 (`/lofi`, `/lofijazz`, `/synthwave`).

---

## 🚀 Installazione e Configurazione

### 1. Requisiti di Sistema
- **Python 3.11+**
- **[FFmpeg](https://ffmpeg.org/download.html)** installato e presente nel `PATH` di sistema.

### 2. Installazione Dipendenze
```bash
# Crea e attiva l'ambiente virtuale
python -m venv venv
source venv/bin/activate  # Su Windows: venv\Scripts\activate

# Installa i pacchetti aggiornati
pip install -r requirements.txt
```

### 3. Configurazione `.env`
Modifica il file `.env` nella cartella principale:
```env
DISCORD_TOKEN='IL_TUO_TOKEN_DISCORD'
OWNER=123456789012345678

# Consigliato (100% Gratuito) per risposte istantanee + Whisper STT in Italiano:
# Ottieni gratis una chiave su https://console.groq.com/keys
GROQ_API_KEY='gsk_...'

# Alternativa gratuita su https://aistudio.google.com/apikey
GEMINI_API_KEY=''

# Voce neurale italiana dell'assistente: elsa | isabella | diego | giuseppe
TTS_VOICE='elsa'

# Opzionali (avanzate)
# GROQ_MODEL=''          # forza un modello Groq (default: il migliore disponibile tra llama-3.3-70b, llama-4, ...)
# GEMINI_MODEL=''        # forza un modello Gemini per le risposte (default: gemini-flash-latest)
# GEMINI_STT_MODEL=''    # forza un modello Gemini per la trascrizione (default: gemini-flash-lite-latest)
# STT_SPECULATIVE=1      # 0 per disattivare la trascrizione anticipata (meno chiamate STT, ~0,4 s più lento)

# Opzionali (necessarie solo per intere playlist/album Spotify)
SPOTIFY_CLIENT_ID=''
SPOTIFY_CLIENT_SECRET=''
SPOTIFY_CLIENT_REFRESH_TOKEN=''
```
> 💡 **Nota**: Il bot funziona **anche senza inserire `GROQ_API_KEY` o `GEMINI_API_KEY`** grazie al motore Google STT (`it-IT`) e al fallback AI Cloud integrato, ma inserire una chiave gratuita **Groq** è consigliato per avere latenza ultra-bassa (~200ms) e trascrizione vocale con **Whisper Large v3 Turbo**.

### 4. Avvio del Bot (Locale)
```bash
python venusbot.py
```

### 🐳 Avvio con Docker / Docker Compose (Consigliato per 24/7)
Il repository include già `Dockerfile` e `docker-compose.yml` con tutte le librerie audio necessarie (`ffmpeg`, `libopus`, `flac`, `libsodium`) e il runtime JavaScript `deno` usato da yt-dlp per YouTube, preconfigurati:

```bash
# 1. Configura il file .env con il tuo DISCORD_TOKEN (e opzionalmente GROQ_API_KEY)
nano .env

# 2. Costruisci e avvia il container in background (24/7 con riavvio automatico)
docker compose up -d --build

# 3. Visualizza i log in tempo reale
docker compose logs -f
```
> La cartella `./management` è montata come volume persistente, quindi la **parola di attivazione** (`/setwakeword`), gli **admin** e i **contatori** rimangono salvati anche se ricrei o aggiorni il container.


---

## 🎙️ Come Usare l'Assistente Vocale in Italiano

Quando il bot è nel canale vocale (tramite `/247` o `/play`), pronuncia la **parola di attivazione** (di default **`Venus`** o **`Venere`**) seguita dalla tua richiesta:

### 🎵 Comandi Musicali Vocali
- *"Venus, metti Blinding Lights"* / *"Venus, riproduci Geolier"* / *"Venus, suona Vasco Rossi"*
- *"Venus, metti una canzone rock anni 80"* *(l'AI sceglierà il brano adatto e lo farà partire!)*
- *"Venus, pausa"* / *"Venus, metti in pausa"* / *"Venus, ferma un attimo"*
- *"Venus, riprendi"* / *"Venus, continua"*
- *"Venus, salta"* / *"Venus, prossima canzone"* / *"Venus, passa alla prossima"* / *"Venus, avanti"*
- *"Venus, stop"* / *"Venus, ferma la musica"* / *"Venus, svuota la coda"*
- *"Venus, ripeti"* / *"Venus, metti in loop"*
- *"Venus, mescola la coda"*
- *"Venus, alza il volume"* / *"Venus, abbassa il volume"* / *"Venus, volume a 50"* / *"Venus, volume trentadue"*
- *"Venus, niente"* / *"Venus, lascia stare"* *(annulla senza risposta)*
- *"Venus, che canzone sta suonando?"*

### 🧠 Domande all'Assistente (Stile Google Assistant)
- *"Venus, che ore sono?"* / *"Venus, che giorno è oggi?"*
- *"Venus, chi ha vinto i mondiali del 2006?"*
- *"Venus, spiegami in modo semplice come funzionano i buchi neri"*
- *"Venus, raccontami una barzelletta"*
- *"Venus, quanto fa 145 per 12?"*

---

## 🛠️ Comandi Slash Principali

### Gestione Parola di Attivazione & Voce (Admin)
- `/setwakeword parola:<nuova_parola> [mantieni_alias:True/False]` — **Imposta subito la nuova parola di attivazione principale**.
- `/wakeword azione:<imposta|aggiungi_alias|rimuovi_alias|lista|reset> [parola:<testo>]` — Gestione completa della parola principale e degli alias secondari.
- `/keywords` — Apre il pannello interattivo con pulsanti per modificare la parola principale e gli alias.
- `/voce voce:<elsa|isabella|diego|giuseppe>` — Cambia la voce italiana dell'assistente.
- `/chiedi domanda:<testo>` — Fai una domanda all'assistente (se è in vocale, risponde a voce nel canale!).

### Comandi Musicali
- `/play`, `/playfile`, `/search`, `/queue`, `/pause`, `/resume`, `/skip`, `/stop`, `/loop`, `/shuffle`, `/volume`, `/time`, `/247`, `/dc`, `/lofi`, `/lofijazz`, `/synthwave`

### Altri Comandi & Admin
- `/transcribe`, `/counters`, `/majorvote`, `/addadmin`, `/removeadmin`, `/ban`, `/unban`, `/sync`, `/restart`, `/help`

---

## 🧪 Test

I test della comprensione dei comandi vocali non richiedono chiavi API né connessione:
```bash
python -m unittest discover -s tests
```
