"""Moteur de nettoyage profond : il ne filtre pas la voix, il la régénère.

C'est ce qui le sépare des deux autres, et ce n'est pas une question de degré.
DeepFilterNet **retire** ce qui n'est pas la voix. MossFormer2 **rehausse** un
signal sale vers un signal propre. Celui-ci **reconstruit la parole** par
diffusion : il produit un audio qui ressemble à ce qui aurait été enregistré
dans de bonnes conditions.

La conséquence est à dire clairement, parce qu'elle décide de l'usage : il peut
s'écarter de ce qui a été prononcé. Là où les deux autres ne peuvent que perdre
du signal, celui-ci peut en **inventer** — une consonne mangée par le bruit
sera restituée telle que le modèle la suppose, pas telle qu'elle a été dite.
Pour une prise irrécupérable c'est ce qui la sauve ; pour une prise correcte
c'est un risque sans contrepartie.

Deux comportements, un seul moteur, et c'est la **route** qui tranche :

    mode = "denoise"   retire le bruit, sans régénérer
    mode = "enhance"   débruite puis reconstruit

Les mettre sur la route et non dans l'image permet à deux versions de modèle de
servir les deux sans deux conteneurs, et empêche une machine de choisir sa
propre qualité.
"""
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import aboengine
import torch
import torchaudio
from fastapi import FastAPI
from pydantic import BaseModel

DEVICE = os.getenv("ABO_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
DEFAULT_MODE = os.getenv("ABO_MODE", "enhance")
# Le nombre de pas du solveur de diffusion. Plus haut rend un résultat plus
# propre et coûte proportionnellement : c'est le seul réglage qui achète de la
# qualité contre du temps, donc il vit sur la route.
DEFAULT_NFE = int(os.getenv("ABO_NFE", "64"))
# Le depot d ou viennent les poids. L amont le code en dur ; on le reprend ici
# pour pouvoir choisir la destination.
REPO_URL = os.getenv("ABO_REPO_URL", "https://huggingface.co/ResembleAI/resemble-enhance")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("abo.resemble")

app = FastAPI(title="ABO — Resemble Enhance")

_loaded = False


class EnhanceRequest(BaseModel):
    # Une des deux formes, jamais les deux (`ADR-016` § 4) : les octets quand le
    # moteur est dans le meme compose, une concession signee quand il est de
    # l'autre cote d'Internet — un `PROXY` pilote, il ne relaie pas.
    audio_b64: str = ""
    audio_url: str = ""
    audio_sha256: str = ""
    config: dict = {}


def _functions():
    """Import différé : charger torch et le modèle au démarrage retarderait
    le `/health` que l'agent interroge avant de se déclarer."""
    from resemble_enhance.enhancer.inference import denoise, enhance

    return denoise, enhance


def run_dir() -> Path:
    """Où vivent les poids — et c'est nous qui le décidons, pas le paquet.

    L'amont ne passe pas par le cache HuggingFace : son `download()` clone un
    dépôt git **dans ses propres `site-packages`**, sans paramètre pour en
    changer. Les poids atterriraient donc dans la couche inscriptible du
    conteneur, à retélécharger à chaque reconstruction, sur le disque même
    qu'on cherche à ménager.

    Ses fonctions d'inférence acceptent en revanche un `run_dir`. On fait donc
    le téléchargement soi-même, une fois, vers le volume monté.
    """
    root = Path(os.getenv("HF_HOME", "/weights"))
    repo = root / "model_repo"
    target = repo / "enhancer_stage2"

    if (target / "hparams.yaml").exists():
        return target

    root.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        logger.info("clonage des poids vers %s", repo)
        subprocess.run(
            ["git", "clone", REPO_URL, str(repo)],
            check=True,
            # Le clone ne tire que les pointeurs ; les octets viennent au
            # `lfs pull` d'après. Sans ce drapeau, git tenterait les deux d'un
            # coup et un échec ne dirait pas lequel a échoué.
            env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
        )
    logger.info("recuperation des gros fichiers (LFS)")
    subprocess.run(["git", "-C", str(repo), "lfs", "pull"], check=True)
    return target


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        # Celui-ci sait nommer son fichier : c'est exactement celui que
        # `run_dir()` cherche pour décider qu'il n'a rien à télécharger. Santé
        # et chargement disent donc la même chose, ce qui est le but.
        "engine": aboengine.weights_present("model_repo/enhancer_stage2/hparams.yaml"),
        "enginePath": "resemble-enhance",
        "device": DEVICE,
        "loaded": _loaded,
        "cuda": torch.cuda.is_available(),
        "mode": DEFAULT_MODE,
    }


@app.post("/enhance")
def enhance_audio(request: EnhanceRequest):
    global _loaded

    try:
        raw = aboengine.source(
            request.audio_b64, request.audio_url, request.audio_sha256, "audio"
        )
        samples, rate = aboengine.read_wav(raw)
    except aboengine.AudioError as failure:
        return aboengine.fail(422, str(failure))

    config = request.config or {}
    mode = config.get("mode", DEFAULT_MODE)
    if mode not in ("denoise", "enhance"):
        # Un mode inconnu est une erreur de configuration de route, pas une
        # panne : le dire vaut mieux que retomber en silence sur un défaut.
        return aboengine.fail(422, f"mode inconnu : {mode}")
    nfe = int(config.get("nfe", DEFAULT_NFE))

    with tempfile.TemporaryDirectory(prefix="abo-re-") as workspace:
        source = Path(workspace) / "in.wav"
        source.write_bytes(raw)

        started = time.monotonic()
        try:
            denoise, enhance = _functions()
            wav, sample_rate = torchaudio.load(str(source))
            # Le modèle attend un canal unique ; une prise stéréo passerait
            # sans erreur et rendrait n'importe quoi.
            wav = wav.mean(dim=0)

            weights = run_dir()
            if mode == "denoise":
                out, out_rate = denoise(wav, sample_rate, DEVICE, run_dir=weights)
            else:
                out, out_rate = enhance(
                    wav, sample_rate, DEVICE, nfe=nfe, solver="midpoint",
                    lambd=0.9, tau=0.5, run_dir=weights,
                )
            _loaded = True
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as failure:
            # Une carte saturée est le cas probable : la diffusion demande plus
            # de mémoire que les deux autres moteurs. `SubprocessError` couvre
            # l'autre cas — un téléchargement de poids qui échoue — et il faut
            # le nommer : il ne descend pas de `RuntimeError`, donc sans lui la
            # panne remonterait en `500` au lieu d'un `502` que le backend sait
            # lire comme « retente ailleurs ».
            logger.warning("%s echoue : %s", mode, failure)
            return aboengine.fail(502, f"{mode} echoue : {failure}")
        elapsed = time.monotonic() - started

    rendered_bytes = aboengine.to_wav(out.cpu().numpy().tolist(), int(out_rate))
    try:
        payload = aboengine.rendered(rendered_bytes, f"resemble-enhance:{mode}")
    except aboengine.AudioError as failure:
        return aboengine.fail(502, str(failure))

    logger.info(
        "%s (nfe=%s) en %.2f s -> pic %s, silences %.1f %%",
        mode,
        nfe,
        elapsed,
        payload["peak"],
        payload["silence_ratio"] * 100,
    )
    return payload
