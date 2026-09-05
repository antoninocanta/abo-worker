#!/usr/bin/env python3
"""Faux moteur, pour eprouver l'agent sans carte graphique ni poids a charger.

Il imite du contrat reel ce qui compte pour l'agent, et rien d'autre : `/health`
et `/synthesize` qui rend un WAV valide en base64. Une seconde d'onde, pas du
silence — une mesure de bout en bout doit porter une verification de contenu,
et un fichier vide passerait tous les controles de format.
"""
import base64
import hashlib
import io
import json
import math
import os
import struct
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("FAKE_ENGINE_PORT", "18100"))
# Un echec a la demande, pour eprouver le chemin de reprise du backend.
FAIL = os.getenv("FAKE_ENGINE_FAIL") == "1"
# Ce que la machine a deja vu passer. Jetable, comme le vrai cache de voix.
CACHE: set[str] = set()


def tone(seconds: float = 1.0, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(index * 0.05)))
                for index in range(int(rate * seconds))
            )
        )
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "engine": True})
        else:
            self._send(404, {"error": "inconnu"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")

        if FAIL:
            self._send(502, {"error": "moteur en panne, volontairement"})
            return
        if self.path == "/enroll":
            self._enroll(request)
        elif self.path == "/synthesize":
            self._synthesize(request)
        elif self.path == "/design":
            self._design(request)
        elif self.path == "/enhance":
            self._enhance(request)
        elif self.path == "/convert":
            self._convert(request)
        else:
            self._send(404, {"error": "inconnu"})

    def _enroll(self, request: dict) -> None:
        """Echantillon -> profil. Le profil est derive de l'echantillon, pas
        tire au hasard : deux enrolements du meme sample doivent rendre la meme
        empreinte, sans quoi le cache ne voudrait rien dire."""
        reference = request.get("reference_b64")
        if not reference:
            self._send(422, {"error": "echantillon manquant"})
            return
        profile = b"FAKEQVOICE" + hashlib.sha256(reference.encode()).digest()
        digest = hashlib.sha256(profile).hexdigest()
        CACHE.add(digest)
        self._send(
            200,
            {
                "voice_b64": base64.b64encode(profile).decode(),
                "sha256": digest,
                "size_bytes": len(profile),
            },
        )

    def _design(self, request: dict) -> None:
        """Description -> extrait. Aucun profil durable : un WAV, rien d'autre."""
        if not request.get("description"):
            self._send(422, {"error": "description manquante"})
            return
        audio = tone(1.5)
        self._send(
            200,
            {
                "audio_b64": base64.b64encode(audio).decode(),
                "format": "wav",
                "size_bytes": len(audio),
            },
        )

    def _transform(self, raw: bytes, gain: float) -> bytes:
        """Rejoue l'entree en changeant son amplitude, et rien d'autre.

        Un faux moteur qui rendrait un ton fabrique passerait tous les
        controles de format **sans avoir lu son entree** : le jour ou
        l'hydratation cesserait d'envoyer l'audio, le test resterait vert. En
        derivant la sortie de l'entree, un pic mesure a la bonne valeur prouve
        que les octets ont fait l'aller-retour.
        """
        with wave.open(io.BytesIO(raw), "rb") as source:
            params = source.getparams()
            frames = source.readframes(source.getnframes())

        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        louder = b"".join(
            struct.pack("<h", max(-32768, min(32767, int(value * gain))))
            for value in samples
        )
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as target:
            target.setparams(params)
            target.writeframes(louder)
        return buffer.getvalue()

    def _enhance(self, request: dict) -> None:
        """Audio -> le meme audio, transforme de facon mesurable."""
        audio = request.get("audio_b64")
        if not audio:
            self._send(422, {"error": "aucun audio a nettoyer"})
            return
        # Le reglage de la route arrive jusqu'ici : c'est ce qui distingue deux
        # versions d'un meme moteur, et un test doit pouvoir le constater.
        mode = (request.get("config") or {}).get("mode", "denoise")
        cleaned = self._transform(base64.b64decode(audio), 0.5)
        self._send(
            200,
            {
                "audio_b64": base64.b64encode(cleaned).decode(),
                "format": "wav",
                "size_bytes": len(cleaned),
                "engine": f"fake-enhance:{mode}",
            },
        )

    def _convert(self, request: dict) -> None:
        """Performance + timbre -> une prise. Les deux entrees sont exigees."""
        performance = request.get("audio_b64")
        if not performance:
            self._send(422, {"error": "aucune performance"})
            return
        if not request.get("reference_b64"):
            # Un moteur de conversion sans timbre de reference ne peut rien
            # rendre : c'est un refus, pas un repli sur une voix par defaut.
            self._send(422, {"error": "aucune voix de reference"})
            return
        converted = self._transform(base64.b64decode(performance), 1.5)
        self._send(
            200,
            {
                "audio_b64": base64.b64encode(converted).decode(),
                "format": "wav",
                "size_bytes": len(converted),
                "engine": "fake-convert",
            },
        )

    def _synthesize(self, request: dict) -> None:
        if not request.get("text"):
            self._send(422, {"error": "texte vide"})
            return

        # Le cache est le coeur du contrat : l'empreinte seule suffit quand la
        # machine a deja le profil, et un `409` explicite sinon.
        sha = request.get("voice_sha256") or ""
        if sha:
            if request.get("voice_b64"):
                CACHE.add(sha)
            elif sha not in CACHE:
                self._send(409, {"error": "VOICE_NOT_CACHED"})
                return

        audio = tone()
        self._send(
            200,
            {
                "audio_b64": base64.b64encode(audio).decode(),
                "format": "wav",
                "size_bytes": len(audio),
                "engine": "fake",
            },
        )

    def log_message(self, fmt: str, *args) -> None:
        print("fake-engine " + (fmt % args), flush=True)


if __name__ == "__main__":
    print(f"fake-engine sur :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
