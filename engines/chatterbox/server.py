"""Moteur de transfert de jeu : le rythme d'une prise, le timbre d'une autre.

C'est le « Follow My Lead » de `specs/00`. L'utilisateur joue une réplique
comme il veut l'entendre — les silences, les accélérations, la respiration
avant un mot — et le moteur la fait redire par une autre voix **en gardant ce
jeu**. Ce n'est pas de la synthèse : rien n'est lu depuis un texte, tout est
repris d'un enregistrement.

Deux entrées, et c'est là qu'est le piège :

    audio_b64      la performance — le jeu, le rythme, l'intention
    reference_b64  le timbre — de qui doit être la voix

Les intervertir ne lève aucune erreur. Le format serait valide, la durée
juste, et le résultat serait la bonne voix disant la mauvaise chose. Le
serveur ne peut pas les distinguer par leur contenu ; c'est le contrat qui les
distingue, et c'est pour ça qu'il est explicite des deux côtés.

La référence est un **échantillon audio**, jamais un `.qvoice` : ce moteur ne
parle pas le format de Qwen. Quand le timbre vient d'une voix ABO, c'est son
échantillon d'origine qui arrive ici — exactement ce pour quoi `ADR-004` exige
qu'il survive à l'artefact.
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

MODEL_RATE = 24000
DEVICE = os.getenv("ABO_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("abo.chatterbox")

app = FastAPI(title="ABO — Chatterbox VC")

# Chargé une fois, gardé vivant. Les poids font plus d'un gigaoctet : les
# recharger à chaque prise transformerait un chapitre en attente pure, et c'est
# la leçon que la résidence de Qwen a déjà payée.
_model = None


class ConvertRequest(BaseModel):
    # Deux entrées de natures opposées, et **deux formes possibles chacune**
    # (`ADR-016` § 4) : les octets quand le moteur est dans le même compose, une
    # concession signée quand il est de l'autre côté d'Internet.
    #
    # C'est l'opération où la référence par URL rapporte le plus : elle a deux
    # entrées pour une sortie, donc relayer en base64 ferait porter au `PROXY`
    # les deux tiers du volume d'un travail.
    audio_b64: str = ""
    audio_url: str = ""
    audio_sha256: str = ""
    reference_b64: str = ""
    reference_url: str = ""
    reference_sha256: str = ""
    config: dict = {}


def model():
    global _model
    if _model is None:
        from chatterbox.vc import ChatterboxVC

        started = time.monotonic()
        logger.info("chargement des poids sur %s", DEVICE)
        _model = ChatterboxVC.from_pretrained(device=DEVICE)
        logger.info("poids charges en %.1f s", time.monotonic() - started)
    return _model


@app.get("/health")
def health() -> dict:
    """Dit si le moteur est **chargeable**, pas seulement si le serveur répond.

    Une machine qui se déclare saine sans porter ses poids rejoindrait la ferme
    et échouerait au premier travail d'un utilisateur.
    """
    return {
        "status": "ok",
        # Chargé, ou chargeable : les poids sont cuits dans l'image, donc leur
        # présence réelle suffit à promettre le premier travail. Répondre vrai
        # sur l'existence du répertoire — qui existe toujours — ferait entrer
        # dans la ferme une machine qui n'a rien à servir.
        "engine": _model is not None or aboengine.weights_present(),
        "enginePath": "chatterbox-vc",
        "device": DEVICE,
        "loaded": _model is not None,
        "cuda": torch.cuda.is_available(),
    }


@app.post("/convert")
def convert(request: ConvertRequest):
    try:
        performance = aboengine.source(
            request.audio_b64, request.audio_url, request.audio_sha256, "audio"
        )
        reference = aboengine.source(
            request.reference_b64,
            request.reference_url,
            request.reference_sha256,
            "reference",
        )
    except aboengine.AudioError as failure:
        return aboengine.fail(422, str(failure))

    # Le modèle travaille en 24 kHz mono. On normalise ici plutôt que de le
    # laisser deviner : une entrée à la mauvaise fréquence rend un résultat
    # plausible et faux.
    try:
        for label, raw in (("performance", performance), ("reference", reference)):
            samples, rate = aboengine.read_wav(raw)
            logger.info("%s : %.2f s a %s Hz", label, len(samples) / rate, rate)
    except aboengine.AudioError as failure:
        return aboengine.fail(422, str(failure))

    with tempfile.TemporaryDirectory(prefix="abo-vc-") as workspace:
        root = Path(workspace)
        source = root / "performance.wav"
        target = root / "timbre.wav"
        source.write_bytes(performance)
        target.write_bytes(reference)

        started = time.monotonic()
        try:
            wav = model().generate(str(source), target_voice_path=str(target))
        except (RuntimeError, OSError, ValueError) as failure:
            # Une carte saturée est le cas le plus probable : 8 Go ne tiennent
            # pas deux modèles résidents. L'échec est explicite pour que le
            # backend retente ailleurs plutôt que de compter un succès vide.
            logger.warning("conversion echouee : %s", failure)
            return aboengine.fail(502, f"conversion echouee : {failure}")
        elapsed = time.monotonic() - started

    audio = wav.squeeze(0).detach().cpu().numpy().tolist()
    rendered_bytes = aboengine.to_wav(audio, MODEL_RATE)

    try:
        payload = aboengine.rendered(rendered_bytes, "chatterbox-vc")
    except aboengine.AudioError as failure:
        return aboengine.fail(502, str(failure))

    logger.info(
        "converti en %.2f s -> pic %s, silences %.1f %%",
        elapsed,
        payload["peak"],
        payload["silence_ratio"] * 100,
    )
    return payload
