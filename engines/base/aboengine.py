"""Ce que tout moteur ABO fait pareil, ecrit une fois.

Trois moteurs de traitement audio partagent exactement deux besoins : ramener
une entree quelconque a la forme que leur modele attend, et **verifier que ce
qui sort est de l'audio**. Les copier trois fois garantirait qu'ils divergent,
et c'est la seconde qui compte le plus : quatre campagnes de mesure sur ce
projet ont rendu de belles durees sur du souffle, avec des fichiers au bon
format et a la bonne longueur.

Un moteur ne connait ni ABO, ni job, ni compte. Ce module non plus.
"""
import base64
import binascii
import hashlib
import io
import os
import struct
import urllib.request
import wave
from pathlib import Path

from fastapi.responses import JSONResponse

# Une concession de lecture est joignable ou elle ne l'est pas. Attendre plus
# longtemps ferait expirer le bail ABO avant que le moteur ait commence.
FETCH_TIMEOUT_SECONDS = 30

# Au-dela, l'entree n'est plus une prise mais une erreur. Le backend borne deja
# la taille d'un travail (`JOB_MAX_AUDIO_SECONDS`) ; cette borne-ci est plus
# large a dessein — un moteur n'est pas l'endroit ou l'on decide de la
# politique de service, il se protege seulement de l'absurde.
MAX_INPUT_BYTES = 200 * 1024 * 1024

# Un echantillon sous ce seuil ne « fait rien ». On cherche l'absence de
# signal, pas la douceur.
SILENCE_THRESHOLD = 64


class AudioError(ValueError):
    """L'entree n'est pas exploitable, et le dire vaut mieux que deviner."""


def fail(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def decode(payload: str) -> bytes:
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as failure:
        raise AudioError("audio_b64 illisible") from failure
    if not raw:
        raise AudioError("audio vide")
    if len(raw) > MAX_INPUT_BYTES:
        raise AudioError("entree trop volumineuse")
    return raw


def encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def source(b64: str = "", url: str = "", sha256: str = "", what: str = "audio") -> bytes:
    """Les octets d'une entree, quelle que soit la forme qui l'annonce.

    Deux formes, et c'est la **distance** qui les separe (`ADR-016` § 4) :

    - `<what>_b64` quand l'agent et le moteur partagent un reseau Docker. Y
      encoder un WAV coute un tiers de volume la ou il ne se paie pas, et le
      moteur reste **sans acces sortant** ;
    - `<what>_url` quand ils sont de part et d'autre d'Internet. Relayer ferait
      alors porter chaque octet par la ligne montante d'un portable domestique
      — 13,4 Mo par minute d'audio, calcules depuis le format — et un `PROXY`
      est cense n'etre qu'un pilote.

    L'URL est une concession signee et bornee : un objet, un verbe, une duree
    courte (`ADR-011`). Le moteur la suit **telle quelle** et n'ajoute aucun
    en-tete : un `x-amz-*` non signe fait refuser toute la requete par R2.

    `sha256` n'est pas decoratif. Sur la forme base64, c'est l'agent qui
    verifiait que le stockage avait bien rendu ce qui etait annonce ; en passant
    une URL on lui retire ce controle, donc le moteur le reprend. Sans ca, la
    garantie disparaitrait en silence — et un resultat calcule sur la mauvaise
    matiere ne se voit dans aucun format de fichier.
    """
    if b64 and url:
        raise AudioError(f"{what} : deux formes a la fois, il en faut une")
    if b64:
        raw = decode(b64)
    elif url:
        raw = _fetch(url, what)
    else:
        raise AudioError(f"{what} absent : ni octets ni reference")

    if sha256:
        digest = hashlib.sha256(raw).hexdigest()
        if digest != sha256.lower():
            raise AudioError(f"{what} : empreinte inattendue")
    return raw


def _fetch(url: str, what: str) -> bytes:
    """Suit une concession de lecture, et refuse tout ce qui n'en est pas une.

    `urllib` plutot qu'un client tiers : c'est un `GET` sur une URL presignee,
    et ajouter une dependance a cinq images pour ca serait payer une
    reconstruction complete a chaque avis de securite du client.
    """
    if not url.startswith("https://"):
        # Une concession voyage en clair sur un lien qu'on n'administre pas. La
        # refuser ici est le seul endroit ou personne ne peut l'oublier.
        raise AudioError(f"{what} : une reference doit etre en https")
    try:
        # `https` est impose juste au-dessus : l'adresse ne peut donc etre ni un
        # `file://` ni un `data:` deguise en concession.
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
            # Un octet de plus que la borne suffit a savoir que c'est trop : on
            # ne lit pas 200 Mo pour decouvrir qu'on en refusait 199.
            raw = response.read(MAX_INPUT_BYTES + 1)
    except OSError as failure:
        raise AudioError(f"{what} : reference injoignable ({failure})") from failure
    if not raw:
        raise AudioError(f"{what} vide")
    if len(raw) > MAX_INPUT_BYTES:
        raise AudioError(f"{what} trop volumineux")
    return raw


def read_wav(raw: bytes) -> tuple[list[int], int]:
    """Rend les echantillons en mono 16 bits, et leur frequence."""
    try:
        with wave.open(io.BytesIO(raw), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (wave.Error, EOFError, OSError) as failure:
        raise AudioError("cette entree n'est pas un WAV lisible") from failure

    if not frames:
        raise AudioError("cette entree ne porte aucun echantillon")
    if width != 2:
        import audioop

        frames = audioop.lin2lin(frames, width, 2)
    if channels > 1:
        import audioop

        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    return list(struct.unpack(f"<{len(frames) // 2}h", frames)), rate


def resample(samples: list[int], source_rate: int, target_rate: int) -> list[int]:
    """Reechantillonne sans numpy ni librosa.

    `audioop` est dans la bibliotheque standard : y ajouter une dependance de
    plusieurs dizaines de mega-octets pour changer une frequence serait cher
    pour ce que c'est. Un modele entraine en 48 kHz nourri en 16 kHz rend un
    resultat plausible et faux — le genre de defaut qui ne se voit dans aucun
    format et ne s'entend qu'a l'ecoute.
    """
    if source_rate == target_rate:
        return samples
    import audioop

    raw = b"".join(struct.pack("<h", value) for value in samples)
    converted, _ = audioop.ratecv(raw, 2, 1, source_rate, target_rate, None)
    return list(struct.unpack(f"<{len(converted) // 2}h", converted))


def to_wav(samples, rate: int) -> bytes:
    """Ecrit un WAV mono 16 bits. Accepte des entiers ou des flottants -1..1."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(
            b"".join(struct.pack("<h", _clamp(value)) for value in samples)
        )
    return buffer.getvalue()


def _clamp(value) -> int:
    if isinstance(value, float) and -1.0 <= value <= 1.0:
        value = value * 32767
    return max(-32768, min(32767, int(value)))


def inspect(raw: bytes) -> dict:
    """La verification de contenu, et c'est le coeur de ce module.

    Un fichier de la bonne taille et de la bonne duree peut ne contenir que du
    souffle. Seule une mesure du signal le dit, et c'est pour cela qu'aucun
    moteur ne rend un resultat sans etre passe par ici.
    """
    samples, rate = read_wav(raw)
    if not samples:
        return {"peak": 0, "silenceRatio": 1.0, "durationSeconds": 0.0}

    peak = max(abs(value) for value in samples)
    quiet = sum(1 for value in samples if abs(value) < SILENCE_THRESHOLD)
    return {
        "peak": peak,
        "silenceRatio": round(quiet / len(samples), 4),
        "durationSeconds": round(len(samples) / rate, 3) if rate else 0.0,
    }


def weights_present(marker: str | None = None, root: str | None = None) -> bool:
    """Y a-t-il vraiment des poids, ou seulement un repertoire vide ?

    `Path.exists()` sur `HF_HOME` ne prouve rien : l'image cree ce repertoire,
    donc il existe toujours. Un moteur qui s'en contentait repondait
    `engine: true` sans rien avoir a servir — et depuis qu'`ABOB-128` fait
    attendre l'agent sur cette reponse, une sante qui ment ne retarde plus
    l'echec, elle le garantit.

    `marker` nomme le fichier qui prouve la presence quand on le connait ; sans
    lui, un seul fichier quelque part sous la racine suffit. La recherche
    s'arrete au premier trouve : un cache HuggingFace porte des milliers
    d'entrees et `/health` est appele toutes les trente secondes.
    """
    base = Path(root or os.getenv("HF_HOME", "/weights"))
    if not base.is_dir():
        return False
    if marker:
        return (base / marker).exists()
    return next((path for path in base.rglob("*") if path.is_file()), None) is not None


def rendered(raw: bytes, engine: str) -> dict:
    """La reponse d'un moteur, avec sa mesure — ou une erreur si c'est muet.

    Rendre du silence n'est jamais un succes : le laisser passer facturerait
    une prise vide et la ferait decouvrir a l'ecoute, des heures plus tard.
    """
    measured = inspect(raw)
    if measured["peak"] == 0:
        raise AudioError("le moteur a rendu du silence")
    return {
        "audio_b64": encode(raw),
        "format": "wav",
        "size_bytes": len(raw),
        "engine": engine,
        "peak": measured["peak"],
        "silence_ratio": measured["silenceRatio"],
        "duration_seconds": measured["durationSeconds"],
    }
