"""L'agent ABO : la machine se declare, tire du travail, rend le resultat.

Tout part d'ici. L'agent **compose vers** le backend en HTTPS et n'ecoute sur
aucun port : c'est ce qui permet a un PC derriere une box domestique de servir
sans toucher au routeur, et a une instance louee pour dix minutes de travailler
sans qu'on lui monte un reseau prive d'abord.

    enrolement  ->  pouls  ->  bail  ->  moteur local  ->  resultat

L'agent ne sait rien des modeles et rien des prix. Il porte des moteurs, il
declare lesquels, et le backend lui attribue ce qu'il reconnait. Ce qui transite
ne reste pas : un texte, un profil de voix, un extrait passent et repartent.

Configuration, par l'environnement :

    ABO_BACKEND_URL      racine de l'API ABO
    ABO_WORKER_KEY       identifiant de la machine, donne a sa creation
    ABO_WORKER_SECRET    secret d'enrolement, affiche une seule fois
    ABO_ENGINES          moteurs portes, separes par des virgules :
                         `engineKey|modelKey|versionNumber|url`
    ABO_WORKER_GPU       carte declaree, quand l'agent ne peut pas la voir
                         lui-meme (il tourne dans son propre conteneur)
    ABO_AGENT_ENGINE_READY_TIMEOUT
                         combien de temps un moteur a pour devenir servable
                         avant que l'agent renonce a rejoindre la ferme
"""
import base64
import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import time

import httpx

# 0.3.0 : l'agent suit des **concessions** au lieu de recevoir des octets
# (`ADR-010` § 6). Le backend lit cette version pour decider laquelle des deux
# formes il sert : une machine qui declare moins que 0.3.0 continue de recevoir
# du base64, donc une image ancienne ne casse pas au premier travail d'un
# utilisateur. Ne pas la baisser sans retirer le code qui va avec.
AGENT_VERSION = "0.3.0"

BACKEND_URL = os.getenv("ABO_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
WORKER_KEY = os.getenv("ABO_WORKER_KEY", "")
WORKER_SECRET = os.getenv("ABO_WORKER_SECRET", "")
ENGINES_SPEC = os.getenv("ABO_ENGINES", "")
DECLARED_GPU = os.getenv("ABO_WORKER_GPU", "")

# Un intervalle court fait vivre le mode Creation : l'utilisateur attend devant
# son ecran, et deux secondes de sondage s'ajoutent a chaque segment. Un bail
# long et un pouls lent suffisent au reste.
POLL_SECONDS = float(os.getenv("ABO_AGENT_POLL_SECONDS", "2"))
HEARTBEAT_SECONDS = float(os.getenv("ABO_AGENT_HEARTBEAT_SECONDS", "30"))
# Le moteur peut mettre des dizaines de secondes a charger ses poids a froid,
# et un chapitre entier bien davantage.
ENGINE_TIMEOUT = float(os.getenv("ABO_AGENT_ENGINE_TIMEOUT", "900"))
BACKEND_TIMEOUT = float(os.getenv("ABO_AGENT_BACKEND_TIMEOUT", "120"))
# Combien de temps on laisse un moteur devenir servable avant de renoncer.
# Large a dessein : depuis `ADR-009` les poids sont cuits dans l'image et la
# reponse est immediate, mais un conteneur qui demarre sur une machine louee
# partage son disque avec le telechargement de l'image voisine.
ENGINE_READY_TIMEOUT = float(os.getenv("ABO_AGENT_ENGINE_READY_TIMEOUT", "600"))
ENGINE_READY_POLL_SECONDS = float(os.getenv("ABO_AGENT_ENGINE_READY_POLL", "3"))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
logger = logging.getLogger("abo.agent")


class Engine:
    """Un moteur local, et la version de modele ABO qu'il sert."""

    def __init__(self, engine_key: str, model_key: str, version_number: int, url: str) -> None:
        self.engine_key = engine_key
        self.model_key = model_key
        self.version_number = version_number
        self.url = url.rstrip("/")

    def declaration(self) -> dict:
        return {
            "engineKey": self.engine_key,
            "modelKey": self.model_key,
            "versionNumber": self.version_number,
        }


def parse_engines(spec: str) -> list[Engine]:
    """`engineKey|modelKey|versionNumber|url`, separes par des virgules.

    Un moteur mal decrit arrete l'agent au demarrage. Se declarer a moitie
    reviendrait a rejoindre la ferme en promettant une capacite qu'on ne sert
    pas, et l'erreur ne se verrait qu'au premier job d'un utilisateur.
    """
    engines: list[Engine] = []
    for entry in (part.strip() for part in spec.split(",")):
        if not entry:
            continue
        fields = [field.strip() for field in entry.split("|")]
        if len(fields) != 4 or not all(fields):
            raise SystemExit(
                f"ABO_ENGINES mal forme : « {entry} ». "
                "Attendu : engineKey|modelKey|versionNumber|url"
            )
        engine_key, model_key, version, url = fields
        if not version.isdigit():
            raise SystemExit(f"Numero de version invalide dans « {entry} ».")
        engines.append(Engine(engine_key, model_key, int(version), url))

    if not engines:
        raise SystemExit("ABO_ENGINES est vide : cette machine n'a rien a servir.")
    return engines


def hardware() -> dict:
    """Ce que la machine dit d'elle-meme.

    Declaratif et non verifie : le backend s'en sert pour placer un travail,
    jamais pour accorder un droit. L'agent vit dans son propre conteneur et ne
    voit pas forcement la carte du moteur — d'ou `ABO_WORKER_GPU`.
    """
    info: dict = {
        "platform": platform.platform(),
        "cpuCount": os.cpu_count(),
        "machine": platform.machine(),
    }
    if DECLARED_GPU:
        info["gpu"] = DECLARED_GPU

    if shutil.which("nvidia-smi"):
        try:
            output = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError) as failure:
            logger.warning("nvidia-smi injoignable : %s", failure)
        else:
            if output:
                name, _, memory = output.splitlines()[0].partition(",")
                info["gpu"] = name.strip()
                info["vram"] = memory.strip()
    return info


def engine_is_ready(client: httpx.Client, engine: "Engine") -> tuple[bool, str]:
    """Interroge `/health` et croit ce qu'il dit du **moteur**, pas du serveur.

    Un serveur HTTP qui repond n'a jamais prouve qu'un modele etait chargeable.
    C'est la distinction que `engines/CONTRACT.md` impose, et le champ `engine`
    est precisement la pour ca.
    """
    try:
        response = client.get(f"{engine.url}/health", timeout=10)
    except httpx.HTTPError as failure:
        return False, f"injoignable ({failure})"
    if response.status_code != 200:
        return False, f"health {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return False, "health illisible"
    if not body.get("engine"):
        return False, "moteur pas encore servable"
    return True, str(body.get("enginePath") or "?")


def wait_for_engines(client: httpx.Client, engines: list["Engine"]) -> None:
    """Ne rejoint la ferme que quand les moteurs savent vraiment travailler.

    S'enroler d'abord et decouvrir ensuite reviendrait a faire entrer dans la
    ferme une machine qui promet une capacite qu'elle ne sert pas — exactement
    ce contre quoi `CONTRACT.md` met en garde, et ce qui se paie le plus cher
    sur une machine **louee** : elle recoit le travail d'un utilisateur, echoue,
    et l'heure est facturee quand meme.

    Renoncer est un arret, pas un avertissement. C'est la meme regle que pour
    un `ABO_ENGINES` mal forme : une machine a moitie capable est pire qu'une
    machine absente, parce que la ferme compte sur elle.
    """
    deadline = time.monotonic() + ENGINE_READY_TIMEOUT
    pending = list(engines)
    reasons: dict[str, str] = {}

    while True:
        still_waiting = []
        for engine in pending:
            ready, detail = engine_is_ready(client, engine)
            if ready:
                logger.info("moteur pret : %s -> %s", engine.engine_key, detail)
            else:
                still_waiting.append(engine)
                reasons[engine.engine_key] = detail

        pending = still_waiting
        if not pending:
            return

        if time.monotonic() >= deadline:
            details = ", ".join(f"{key} : {why}" for key, why in sorted(reasons.items()))
            raise SystemExit(
                f"Moteurs toujours pas servables apres {ENGINE_READY_TIMEOUT:.0f} s "
                f"— {details}. Cette machine ne rejoint pas la ferme."
            )

        logger.info(
            "en attente de %s moteur(s) : %s",
            len(pending),
            ", ".join(f"{e.engine_key} ({reasons[e.engine_key]})" for e in pending),
        )
        time.sleep(ENGINE_READY_POLL_SECONDS)


class Backend:
    """Le seul interlocuteur distant de l'agent, toujours en sortant."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._base = f"{BACKEND_URL}/v1/workers/{WORKER_KEY}"
        self._headers = {"X-Worker-Secret": WORKER_SECRET}

    def _post(self, path: str, payload: dict) -> httpx.Response:
        return self._client.post(
            self._base + path, json=payload, headers=self._headers, timeout=BACKEND_TIMEOUT
        )

    def enrol(self, engines: list[Engine]) -> None:
        response = self._post(
            "/enrol",
            {
                "agentVersion": AGENT_VERSION,
                "hardware": hardware(),
                "engines": [engine.declaration() for engine in engines],
            },
        )
        if response.status_code == 403:
            raise SystemExit("Cette machine a ete revoquee. Arret.")
        if response.status_code == 422:
            # Un moteur que le backend ne connait pas n'est pas une capacite
            # ignoree en silence : c'est une machine mal configuree.
            raise SystemExit(f"Declaration refusee : {response.text}")
        response.raise_for_status()
        logger.info("enrole : %s", response.json())

    def heartbeat(self, running: int) -> dict:
        response = self._post("/heartbeat", {"load": {"running": running}})
        if response.status_code == 403:
            raise SystemExit("Cette machine a ete revoquee. Arret.")
        response.raise_for_status()
        return response.json()

    def lease(self) -> dict | None:
        response = self._post("/lease", {})
        if response.status_code == 204:
            return None
        if response.status_code == 409:
            # La machine n'est plus attribuable : elle devra se redeclarer.
            logger.warning("bail refuse : %s", response.text)
            return None
        response.raise_for_status()
        return response.json()

    def voice(self, sha256: str) -> str:
        """Va chercher un profil que cette machine n'a pas encore."""
        response = self._client.get(
            f"{self._base}/voices/{sha256}",
            headers=self._headers,
            timeout=BACKEND_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()["voiceB64"]

    def fetch(self, grant: dict) -> bytes:
        """Suit une concession de lecture, et rend les octets.

        La concession dit **quelle requete faire** — verbe, adresse, en-tetes —
        et l'agent ne fait que la suivre. C'est ce qui permet au backend de
        changer le chemin des octets sans toucher a ce code : aujourd'hui
        l'adresse est une route d'ABO, demain un stockage objet, et l'agent ne
        verra pas la difference.

        Le secret de la machine n'est ajoute que sur une adresse **relative**,
        c'est-a-dire sur notre propre API. Une adresse absolue designe un tiers,
        et lui presenter ce secret le lui donnerait.
        """
        url = grant["url"]
        headers = dict(grant.get("headers") or {})
        if url.startswith("/"):
            url = BACKEND_URL + url
            headers.update(self._headers)
        response = self._client.request(
            grant.get("method", "GET"), url, headers=headers, timeout=BACKEND_TIMEOUT
        )
        response.raise_for_status()
        return response.content

    def deposit(
        self,
        grant: dict,
        attempt: int,
        payload: bytes,
        content_type: str,
        kind: str = "output",
    ) -> dict:
        """Depose des octets bruts, et rend la reference que le backend frappe.

        Plus de base64 sur ce chemin : c'est le tiers de volume qu'il coutait,
        et il n'achetait rien qu'un corps JSON.
        """
        url = grant["url"]
        headers = dict(grant.get("headers") or {})
        if url.startswith("/"):
            url = BACKEND_URL + url
            headers.update(self._headers)
        headers["Content-Type"] = content_type
        response = self._client.request(
            grant.get("method", "POST"),
            url,
            params={"attempt": attempt, "kind": kind},
            content=payload,
            headers=headers,
            timeout=BACKEND_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def result(self, job_id: str, attempt: int, payload: dict) -> None:
        response = self._post(f"/jobs/{job_id}/result", {**payload, "attempt": attempt})
        response.raise_for_status()
        logger.info("rendu job=%s -> %s", job_id, response.json().get("status"))

    def failure(self, job_id: str, attempt: int, error_class: str, detail: str) -> None:
        response = self._post(
            f"/jobs/{job_id}/failure",
            {"attempt": attempt, "errorClass": error_class, "detail": detail[:500]},
        )
        response.raise_for_status()
        logger.info("echec signale job=%s -> %s", job_id, response.json().get("status"))


class EngineError(RuntimeError):
    """Le moteur local a refuse ou n'a rien rendu."""


def audio_from(job_input: dict, name: str, backend: "Backend") -> str | None:
    """L'audio d'une entree, en base64, quelle que soit la forme du bail.

    Deux formes coexistent le temps d'une version (`ADR-010` § 6) : l'ancienne
    porte les octets sous `audioB64` / `referenceB64`, la nouvelle porte une
    reference et une concession sous `audio` / `reference`. L'agent les reduit
    ici a une seule chose, et les moteurs n'en savent rien.

    **Le base64 ne disparait pas, il change de longueur de fil.** Les moteurs
    parlent JSON sur `127.0.0.1` : y encoder un WAV coute un tiers de volume
    sur une boucle locale, ou il ne se paie pas. Ce qui coutait cher etait le
    meme tiers sur un lien montant domestique, et c'est celui-la qui part.
    """
    ancien = job_input.get(name + "B64")
    if ancien:
        return ancien
    reference = job_input.get(name)
    if not reference or not reference.get("grant"):
        return None
    octets = backend.fetch(reference["grant"])
    annonce = reference.get("sha256")
    if annonce and hashlib.sha256(octets).hexdigest() != annonce:
        # La concession a rendu autre chose que ce qu'on annoncait. Echouer
        # ici rend le travail a la file ; le passer au moteur produirait un
        # resultat sur la mauvaise matiere, et personne ne le verrait.
        raise EngineError("Les octets recus ne correspondent pas a leur empreinte.")
    return base64.b64encode(octets).decode("ascii")


def _post_engine(client: httpx.Client, url: str, body: dict) -> httpx.Response:
    return client.post(url, json=body, timeout=ENGINE_TIMEOUT)


def synthesize(
    client: httpx.Client,
    engine: Engine,
    job_input: dict,
    backend: "Backend",
    config: dict,
) -> dict:
    """Texte -> WAV. Le profil de voix appartient au backend, pas a la machine.

    Seule l'**empreinte** arrive avec le travail : renvoyer 25 Mo a chaque
    segment d'un chapitre serait absurde. Si la machine n'a pas encore ce
    profil, le moteur le dit (`409 VOICE_NOT_CACHED`) au lieu de le deviner, et
    on va le chercher une fois. Les segments suivants passent par le cache.
    """
    body = {
        "text": job_input.get("text", ""),
        "language": job_input.get("language", "French"),
        "instruction": job_input.get("instruction", "") or "",
        "emotion": job_input.get("emotion", "") or "",
        "preset_voice": job_input.get("presetVoice", "") or "",
        "voice_sha256": job_input.get("voiceSha256", "") or "",
    }

    response = _post_engine(client, f"{engine.url}/synthesize", body)
    if response.status_code == 409 and body["voice_sha256"]:
        # Le chemin lent, et il doit le rester : une fois par machine et par
        # voix. Le profil ne devient pas durable ici, il alimente un cache.
        logger.info("profil absent du cache, recuperation : %s", body["voice_sha256"][:12])
        body["voice_b64"] = backend.voice(body["voice_sha256"])
        response = _post_engine(client, f"{engine.url}/synthesize", body)

    if response.status_code != 200:
        raise EngineError(f"{response.status_code} {response.text[:300]}")

    payload = response.json()
    audio = payload.get("audio_b64")
    if not audio:
        raise EngineError("Le moteur n'a rendu aucun audio.")
    return {
        "audioB64": audio,
        "format": payload.get("format", "wav"),
        # Ce que la machine a mesure. Sert au cout interne et au placement ;
        # le prix, lui, est recalcule par le backend a partir de l'entree.
        "metrics": {
            "sizeBytes": payload.get("size_bytes", 0),
            "enginePath": payload.get("engine", "unknown"),
        },
    }


def enrol_voice(
    client: httpx.Client,
    engine: Engine,
    job_input: dict,
    backend: "Backend",
    config: dict,
) -> dict:
    """Echantillon + transcription -> profil de voix.

    Le profil repart vers le backend, a qui la voix appartient. Ce qui reste
    ici n'est qu'un cache, jetable : le perdre ne coute qu'un renvoi.
    """
    reference = audio_from(job_input, "reference", backend)
    if not reference:
        raise EngineError("Aucun echantillon de reference dans ce travail.")

    response = _post_engine(
        client,
        f"{engine.url}/enroll",
        {
            "reference_b64": reference,
            "voice_name": job_input.get("voiceName", "voix"),
            "language": job_input.get("language", "French"),
            "reference_text": job_input.get("referenceText", "") or "",
        },
    )
    if response.status_code != 200:
        raise EngineError(f"{response.status_code} {response.text[:300]}")

    payload = response.json()
    profile = payload.get("voice_b64")
    if not profile:
        raise EngineError("Le moteur n'a rendu aucun profil de voix.")
    return {
        "artifactB64": profile,
        # Le backend recalcule l'empreinte : celle-ci ne sert qu'a detecter une
        # corruption de transport.
        "artifactSha256": payload.get("sha256", ""),
        "metrics": {"sizeBytes": payload.get("size_bytes", 0)},
    }


def design_voice(
    client: httpx.Client,
    engine: Engine,
    job_input: dict,
    backend: "Backend",
    config: dict,
) -> dict:
    """Description ecrite -> extrait audio d'une voix inventee.

    Ce mode ne produit **pas** de profil durable : le moteur rend un WAV, pas un
    `.qvoice`. L'extrait valide devient ensuite l'echantillon de reference d'un
    clonage, et c'est la seulement que la voix devient une voix.
    """
    description = job_input.get("description", "")
    if not description:
        raise EngineError("Aucune description dans ce travail.")

    response = _post_engine(
        client,
        f"{engine.url}/design",
        {
            "description": description,
            "text": job_input.get("text", ""),
            "language": job_input.get("language", "French"),
        },
    )
    if response.status_code == 501:
        # L'image ne porte pas les poids VoiceDesign. C'est une machine mal
        # equipee, pas une panne : le backend retentera ailleurs.
        raise EngineError("VoiceDesign absent de cette image.")
    if response.status_code != 200:
        raise EngineError(f"{response.status_code} {response.text[:300]}")

    payload = response.json()
    audio = payload.get("audio_b64")
    if not audio:
        raise EngineError("Le moteur n'a rendu aucun extrait.")
    return {
        "audioB64": audio,
        "format": payload.get("format", "wav"),
        "metrics": {"sizeBytes": payload.get("size_bytes", 0)},
    }


def enhance_audio(
    client: httpx.Client,
    engine: Engine,
    job_input: dict,
    backend: "Backend",
    config: dict,
) -> dict:
    """Une prise bruitee -> la meme prise, nettoyee.

    Trois moteurs servent cette operation et rendent trois qualites : un filtre
    rapide sur processeur, un rehaussement fidele en 48 kHz, une regeneration
    qui reconstruit la parole. L'agent n'arbitre pas entre eux — le backend a
    deja resolu quelle version execute ce travail, et la machine ne porte que
    ce qu'elle a declare.

    Ce que `config` transporte vient de la **route**, jamais de cette machine :
    c'est ainsi qu'un meme moteur peut debruiter ici et regenerer la, sans deux
    images ni deux cles de moteur.
    """
    audio = audio_from(job_input, "audio", backend)
    if not audio:
        raise EngineError("Aucun audio a nettoyer dans ce travail.")

    response = _post_engine(
        client, f"{engine.url}/enhance", {"audio_b64": audio, "config": config}
    )
    if response.status_code != 200:
        raise EngineError(f"{response.status_code} {response.text[:300]}")

    payload = response.json()
    cleaned = payload.get("audio_b64")
    if not cleaned:
        raise EngineError("Le moteur n'a rendu aucun audio.")
    return {
        "audioB64": cleaned,
        "format": payload.get("format", "wav"),
        "metrics": {
            "sizeBytes": payload.get("size_bytes", 0),
            "enginePath": payload.get("engine", "unknown"),
        },
    }


def transfer_performance(
    client: httpx.Client,
    engine: Engine,
    job_input: dict,
    backend: "Backend",
    config: dict,
) -> dict:
    """Le jeu d'une prise, le timbre d'une autre.

    Deux entrees, et les confondre rendrait la bonne voix disant la mauvaise
    chose : `audioB64` porte la **performance** — le rythme, l'intention, les
    respirations — et `referenceB64` porte le **timbre** a lui preter.

    La reference est un echantillon audio et jamais un `.qvoice` : ce moteur ne
    parle pas le format de Qwen. C'est le backend qui choisit lequel envoyer,
    et il envoie l'echantillon d'origine — celui qu'`ADR-004` exige de garder
    precisement pour qu'un autre moteur puisse le lire.
    """
    performance = audio_from(job_input, "audio", backend)
    reference = audio_from(job_input, "reference", backend)
    if not performance:
        raise EngineError("Aucune performance dans ce travail.")
    if not reference:
        raise EngineError("Aucune voix de reference dans ce travail.")

    response = _post_engine(
        client,
        f"{engine.url}/convert",
        {"audio_b64": performance, "reference_b64": reference, "config": config},
    )
    if response.status_code != 200:
        raise EngineError(f"{response.status_code} {response.text[:300]}")

    payload = response.json()
    converted = payload.get("audio_b64")
    if not converted:
        raise EngineError("Le moteur n'a rendu aucun audio.")
    return {
        "audioB64": converted,
        "format": payload.get("format", "wav"),
        "metrics": {
            "sizeBytes": payload.get("size_bytes", 0),
            "enginePath": payload.get("engine", "unknown"),
        },
    }


HANDLERS = {
    "TTS": synthesize,
    "VOICE_CLONE": enrol_voice,
    "VOICE_DESIGN": design_voice,
    "AUDIO_ENHANCE": enhance_audio,
    "PERFORMANCE_TRANSFER": transfer_performance,
}


def execute(
    client: httpx.Client, engines: dict[str, Engine], assignment: dict, backend: "Backend"
) -> dict:
    engine = engines.get(assignment["engineKey"])
    if engine is None:
        # Le backend a attribue un travail pour un moteur que cette machine ne
        # porte pas : sa declaration et sa realite ont diverge.
        raise EngineError(f"Moteur non porte : {assignment['engineKey']}")

    handler = HANDLERS.get(assignment["operation"])
    if handler is None:
        raise EngineError(f"Operation non servie par cet agent : {assignment['operation']}")

    # Le reglage vient de la route. Un backend plus ancien n'en envoie pas :
    # l'absence vaut « rien de particulier », pas une erreur.
    config = assignment.get("engineConfig") or {}

    started = time.monotonic()
    payload = handler(client, engine, assignment.get("input") or {}, backend, config)
    payload["metrics"]["computeMs"] = int((time.monotonic() - started) * 1000)
    return _deposit_output(payload, assignment, backend)


def _deposit_output(payload: dict, assignment: dict, backend: "Backend") -> dict:
    """Depose les octets produits, et ne garde qu'une reference a rendre.

    Le bail dit ou deposer. S'il ne le dit pas, ce backend sert encore
    l'ancienne forme et le resultat repart tel quel — c'est ce qui permet a cet
    agent de parler aux deux, le temps que le deploiement rattrape.

    Ce qu'un `artifactB64` porte est un profil de voix de 25 Mo : c'est le plus
    gros objet du systeme, et celui pour lequel `ADR-010` a ete ecrit.
    """
    deposit = assignment.get("deposit")
    if not deposit:
        return payload

    attempt = assignment["attempt"]
    for cle_b64, cle_media, content_type, kind in (
        ("audioB64", "outputMediaId", "audio/wav", "output"),
        ("artifactB64", "artifactMediaId", "application/octet-stream", "artifact"),
    ):
        encode = payload.pop(cle_b64, None)
        if not encode:
            continue
        octets = base64.b64decode(encode)
        depose = backend.deposit(deposit, attempt, octets, content_type, kind)
        payload[cle_media] = depose["mediaId"]
        if cle_media == "outputMediaId":
            payload["outputSha256"] = depose["sha256"]
    return payload


def run() -> None:
    if not WORKER_KEY or not WORKER_SECRET:
        raise SystemExit("ABO_WORKER_KEY et ABO_WORKER_SECRET sont requis.")

    engines = parse_engines(ENGINES_SPEC)
    by_key = {engine.engine_key: engine for engine in engines}
    logger.info(
        "agent %s -> %s, moteurs : %s",
        AGENT_VERSION,
        BACKEND_URL,
        ", ".join(f"{e.engine_key}@{e.model_key}v{e.version_number}" for e in engines),
    )

    with httpx.Client() as client:
        backend = Backend(client)
        # Les moteurs d'abord, la ferme ensuite. L'ordre est le sujet
        # d'`ABOB-128` : declarer une capacite avant de l'avoir est une panne
        # qu'on fait decouvrir a un utilisateur.
        wait_for_engines(client, engines)
        backend.enrol(engines)

        last_heartbeat = 0.0
        while True:
            try:
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_SECONDS:
                    pulse = backend.heartbeat(running=0)
                    last_heartbeat = now
                    if pulse.get("mustEnrol"):
                        # Le backend l'avait declaree perdue : ses capacites
                        # datent d'avant sa disparition, elle se redeclare.
                        # Et elle repasse par la meme porte — si elle a ete
                        # declaree perdue parce que son moteur est tombe, se
                        # redeclarer sans verifier la remettrait en ligne
                        # exactement aussi cassee qu'avant.
                        logger.info("redeclaration demandee")
                        wait_for_engines(client, engines)
                        backend.enrol(engines)

                assignment = backend.lease()
                if assignment is None:
                    time.sleep(POLL_SECONDS)
                    continue

                job_id = assignment["jobId"]
                # Le bail vaut pour cette tentative-la : le backend refuse un
                # resultat rendu sur une tentative qu'il a declaree perdue.
                attempt = assignment["attempt"]
                logger.info(
                    "job=%s operation=%s tentative=%s",
                    job_id,
                    assignment["operation"],
                    attempt,
                )
                try:
                    payload = execute(client, by_key, assignment, backend)
                except (EngineError, httpx.HTTPError) as failure:
                    # Un travail qu'on ne sait pas faire se rend tout de suite :
                    # le backend le retentera ailleurs sans attendre le bail.
                    logger.warning("job=%s echec : %s", job_id, failure)
                    backend.failure(job_id, attempt, type(failure).__name__, str(failure))
                else:
                    backend.result(job_id, attempt, payload)

            except SystemExit:
                raise
            except httpx.HTTPError as failure:
                # Le backend est injoignable. On patiente : une coupure reseau
                # ne doit pas sortir une machine de la ferme.
                logger.warning("backend injoignable : %s", failure)
                time.sleep(POLL_SECONDS * 5)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logger.info("arret demande")
