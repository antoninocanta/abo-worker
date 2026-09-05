"""Moteur de rehaussement fidèle : MossFormer2, 48 kHz.

La différence avec DeepFilterNet tient en une phrase, et elle décide de quel
outil on prend : DeepFilterNet **filtre** — il retire ce qui n'est pas la voix
et ne touche jamais à la voix elle-même. MossFormer2 **rehausse** — il
reconstruit un signal propre à partir d'un signal sale, à 48 kHz, et récupère
des prises que le filtre laisserait sourdes.

Ce qui sort reste ce qui a été dit. C'est ce qui le sépare de Resemble Enhance,
qui régénère la parole et peut donc s'en écarter.

Le point de vigilance est la fréquence : le modèle porte `48K` dans son nom
parce qu'il a été entraîné là. Nourri en 16 kHz, il rend un résultat plausible
et faux — bon format, bonne durée, et une voix qui sonne mal sans qu'aucune
erreur ne soit levée. Le serveur normalise donc avant, toujours.
"""
import logging
import os
import tempfile
import time
from pathlib import Path

import aboengine
import torch
from fastapi import FastAPI
from pydantic import BaseModel

MODEL_RATE = 48000
DEFAULT_CHECKPOINT = os.getenv("ABO_CHECKPOINT", "MossFormer2_SE_48K")
DEVICE = os.getenv("ABO_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("abo.clearervoice")

app = FastAPI(title="ABO — ClearerVoice")

# Un modèle par checkpoint, gardé vivant. Sur une carte de 8 Go, en garder
# plusieurs ne tiendrait pas : la ferme ne fait tourner qu'un moteur à la fois
# de toute façon.
_models: dict = {}


class EnhanceRequest(BaseModel):
    # Une des deux formes, jamais les deux (`ADR-016` § 4) : les octets quand le
    # moteur est dans le meme compose, une concession signee quand il est de
    # l'autre cote d'Internet — un `PROXY` pilote, il ne relaie pas.
    audio_b64: str = ""
    audio_url: str = ""
    audio_sha256: str = ""
    # Forme differee (`ADR-016` § 4) : la sortie reste ici et le porteur
    # n'en recoit que la designation. Mis par l'agent quand le moteur est de
    # l'autre cote d'Internet.
    defer_output: bool = False
    config: dict = {}


def model(checkpoint: str):
    if checkpoint not in _models:
        from clearvoice import ClearVoice

        started = time.monotonic()
        logger.info("chargement de %s sur %s", checkpoint, DEVICE)
        _models[checkpoint] = ClearVoice(
            task="speech_enhancement", model_names=[checkpoint]
        )
        logger.info("poids charges en %.1f s", time.monotonic() - started)
    return _models[checkpoint]


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        # Voir `chatterbox/server.py` : l'existence du répertoire de poids ne
        # prouve rien, l'image le crée. Il faut des octets dedans.
        "engine": bool(_models) or aboengine.weights_present(),
        "enginePath": DEFAULT_CHECKPOINT,
        "device": DEVICE,
        "loaded": sorted(_models),
        "cuda": torch.cuda.is_available(),
        "sampleRate": MODEL_RATE,
    }


@app.post("/enhance")
def enhance(request: EnhanceRequest):
    try:
        raw = aboengine.source(
            request.audio_b64, request.audio_url, request.audio_sha256, "audio"
        )
        samples, rate = aboengine.read_wav(raw)
    except aboengine.AudioError as failure:
        return aboengine.fail(422, str(failure))

    # `checkpoint` vient de la **route**, pas de cette machine : c'est ainsi que
    # deux versions de modèle peuvent servir la même opération sans deux images.
    checkpoint = (request.config or {}).get("checkpoint", DEFAULT_CHECKPOINT)

    prepared = aboengine.to_wav(
        aboengine.resample(samples, rate, MODEL_RATE), MODEL_RATE
    )
    logger.info("entree %.2f s a %s Hz -> %s Hz", len(samples) / rate, rate, MODEL_RATE)

    # L'entrée passe par un fichier — la bibliothèque ne sait lire que ça — mais
    # **la sortie est reprise du tableau rendu**, pas d'un fichier écrit.
    # `write()` délègue à un état interne du modèle plutôt qu'aux résultats
    # qu'on lui passe, et avec `online_write=False` il n'écrit rien : le moteur
    # tournait, puis on cherchait un fichier qui n'existerait jamais. Lire le
    # tableau évite aussi de dépendre du nom que la bibliothèque choisit, qui a
    # déjà changé entre versions.
    with tempfile.TemporaryDirectory(prefix="abo-cv-") as workspace:
        source = Path(workspace) / "in.wav"
        source.write_bytes(prepared)

        started = time.monotonic()
        try:
            output = model(checkpoint)(input_path=str(source), online_write=False)
        except (RuntimeError, OSError, ValueError, KeyError) as failure:
            logger.warning("rehaussement echoue : %s", failure)
            return aboengine.fail(502, f"rehaussement echoue : {failure}")
        elapsed = time.monotonic() - started

    values = output[0] if getattr(output, "ndim", 1) > 1 else output
    cleaned = aboengine.to_wav(values.tolist(), MODEL_RATE)

    try:
        payload = aboengine.rendered(cleaned, f"clearervoice:{checkpoint}", request.defer_output)
    except aboengine.AudioError as failure:
        return aboengine.fail(502, str(failure))

    logger.info(
        "rehausse en %.2f s -> pic %s, silences %.1f %%",
        elapsed,
        payload["peak"],
        payload["silence_ratio"] * 100,
    )
    return payload


# `/upload` et `/drop`, identiques sur tous les moteurs (`ADR-016` § 4).
aboengine.register_deposit_routes(app)
