"""
Test della comprensione dei comandi vocali (nessuna chiamata di rete).
Esegui con:  python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from management.bot_keywords import BotKeywords  # noqa: E402
from scripts.ai_assistant import AIAssistant, parse_italian_number  # noqa: E402

WAKE_CONFIG = {"primary": "venus", "aliases": ["venere"], "fuzzy_threshold": 0.78}


class TestComandiLocali(unittest.TestCase):
    def setUp(self):
        self.assistant = AIAssistant()

    def assertAction(self, text, action, query=None):
        resp = self.assistant._match_fast_command(text)
        self.assertIsNotNone(resp, f"{text!r} non riconosciuto in locale")
        self.assertEqual(resp.action, action, text)
        if query is not None:
            self.assertEqual(resp.query, query, text)

    def test_punteggiatura_di_whisper(self):
        self.assertAction("Metti in pausa, per favore.", "pause")
        self.assertAction("Salta, grazie.", "skip")
        self.assertAction("Riprendi la musica, grazie!", "resume")
        self.assertAction("Stop.", "stop")

    def test_comandi_che_prima_diventavano_canzoni(self):
        self.assertAction("metti pausa", "pause")
        self.assertAction("Metti la prossima.", "skip")
        self.assertAction("Ferma un attimo", "pause")

    def test_varianti_naturali(self):
        self.assertAction("Passa alla prossima", "skip")
        self.assertAction("Canzone successiva", "skip")
        self.assertAction("Fermati", "stop")
        self.assertAction("Sì, salta questa canzone", "skip")
        self.assertAction("Cos'è questa canzone?", "now_playing")
        self.assertAction("Che canzone stai suonando?", "now_playing")
        self.assertAction("Rimetti la canzone dall'inizio", "replay")
        self.assertAction("Niente, lascia stare.", "none")
        self.assertAction("Ehm...", "listen_followup")
        self.assertAction("Metti una canzone.", "listen_song_followup")

    def test_volume(self):
        self.assertAction("Volume al trenta per cento", "volume_set", "30")
        self.assertAction("Volume trentadue", "volume_set", "32")
        self.assertAction("Abbassa a 20", "volume_set", "20")
        self.assertAction("Alza al massimo", "volume_set", "100")
        self.assertAction("Alza un po il volume", "volume_up")
        self.assertAction("Più forte", "volume_up")
        self.assertAction("Piu piano", "volume_down")

    def test_numeri_italiani(self):
        for word, value in {
            "ventuno": 21, "ventotto": 28, "ventitré": 23, "quarantacinque": 45,
            "cento": 100, "centoventi": 120, "metà": 50, "75": 75,
        }.items():
            self.assertEqual(parse_italian_number(word), value, word)
        self.assertIsNone(parse_italian_number("canzone"))

    def test_estrazione_canzone(self):
        extract = AIAssistant._extract_Explicit_play_query
        self.assertEqual(extract("Allora, metti Vasco Rossi."), "Vasco Rossi")
        self.assertEqual(extract("Mettimi Blinding Lights"), "Blinding Lights")
        self.assertEqual(extract("Voglio sentire Geolier"), "Geolier")
        self.assertEqual(extract("Fammi sentire qualcosa dei Queen"), "Queen")
        self.assertEqual(extract("Riproduci un po' di musica rock"), "musica rock")
        self.assertIsNone(extract("Cerca su Google chi è il presidente"))


class TestParolaDiAttivazione(unittest.TestCase):
    def setUp(self):
        self.kw = BotKeywords()

    def extract(self, text):
        return self.kw.extract_command_sync(text, WAKE_CONFIG)

    def test_attivazioni(self):
        self.assertEqual(self.extract("Venus, metti in pausa."), (True, "venus", "metti in pausa."))
        self.assertEqual(self.extract("Ehi Venus metti Vasco"), (True, "venus", "metti Vasco"))
        self.assertEqual(self.extract("VÉNUS, alza il volume")[2], "alza il volume")
        self.assertEqual(self.extract("venusmetti in pausa")[2], "metti in pausa")
        self.assertEqual(self.extract("Ve nus metti Blinding Lights")[2], "metti Blinding Lights")
        self.assertTrue(self.extract("Ciao Venere, che ore sono?")[0])

    def test_nessuna_attivazione(self):
        for text in ("ieri sera ho mangiato una pizza", "venerdì andiamo al mare", "menu del giorno", "Venusiano"):
            self.assertFalse(self.extract(text)[0], text)

    def test_ipotesi_alternative(self):
        activated, _, remainder = self.extract("metti Vasco [EN_HINT: Venus put Vasco | venus metti vasco]")
        self.assertTrue(activated)
        self.assertTrue(remainder.startswith("metti vasco"))


if __name__ == "__main__":
    unittest.main()
