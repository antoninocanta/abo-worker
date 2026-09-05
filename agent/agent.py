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
    ABO_VAST_API_KEY     cle du compte Vast, requise uniquement par un relais
                         dont une URL moteur commence par vast+serverless://
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
import ssl
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx

# Le backend lit cette version pour decider quelle forme de contrat il sert. Une
# machine qui declare moins continue de recevoir l'ancienne, donc une image
# ancienne ne casse pas au premier travail d'un utilisateur — sur une location,
# l'heure est facturee quand meme. Ne pas la baisser sans retirer le code qui va
# avec.
#
#   0.3.0  l'entree d'un job arrive en **concession** et non en octets, et le
#          resultat se depose en octets bruts (`ADR-010` § 6, `ABOB-136`)
#   0.4.0  le **profil de voix** arrive lui aussi en concession (`ABOB-137`).
#          C'est le seul transfert du projet qui pese 25 Mo, et le seul ou
#          quelqu'un attend devant l'ecran. Un depot vers une adresse absolue
#          suit desormais la concession telle quelle, sans y ajouter de
#          parametre : une URL presignee signe sa propre query.
#   0.5.0  la concession de depot se **demande quand les octets existent**
#          (`ADR-013`, `ABOB-147`), en annoncant nature, taille et empreinte.
#          Sans ca, une seule concession sert deux natures : un `.qvoice`
#          atterrirait dans le stockage de travail avec un TTL de 14 jours.
#   0.6.0  un agent de confiance peut relayer les deux operations sans etat
#          vers Vast **apres** avoir recu un bail ABO normal (`ABOB-133`).
#   0.7.0  chaque capacite declare **ou elle calcule** — `LOCAL_GPU`,
#          `LOCAL_CPU` ou `PROXY` (`ADR-016` § 1, `ABOB-163`). Cinquieme champ
#          optionnel d'`ABO_ENGINES`. Une machine peut donc porter un `PROXY`
#          vers une capacite louee a cote d'un moteur local, et le backend le
#          sait enfin : sans ce champ il rangeait tout en local et un differe
#          pouvait partir sur une capacite facturee a l'appel.
AGENT_VERSION = "0.7.0"

BACKEND_URL = os.getenv("ABO_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
WORKER_KEY = os.getenv("ABO_WORKER_KEY", "")
WORKER_SECRET = os.getenv("ABO_WORKER_SECRET", "")
ENGINES_SPEC = os.getenv("ABO_ENGINES", "")
DECLARED_GPU = os.getenv("ABO_WORKER_GPU", "")
VAST_API_KEY = os.getenv("ABO_VAST_API_KEY", "")
VAST_CONSOLE_URL = os.getenv("ABO_VAST_CONSOLE_URL", "https://console.vast.ai").rstrip(
    "/"
)
VAST_SERVERLESS_URL = os.getenv(
    "ABO_VAST_SERVERLESS_URL", "https://run.vast.ai"
).rstrip("/")

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
# Le bail ABO vaut dix minutes. Le routeur n'en consomme au plus que la moitie,
# afin qu'un worker froid garde encore le temps de calculer et de rendre.
VAST_ROUTE_TIMEOUT = float(os.getenv("ABO_VAST_ROUTE_TIMEOUT", "300"))
VAST_ROUTE_POLL_SECONDS = float(os.getenv("ABO_VAST_ROUTE_POLL_SECONDS", "2"))

STATELESS_OPERATIONS = frozenset({"AUDIO_ENHANCE", "PERFORMANCE_TRANSFER"})
SERVERLESS_ROUTE_BY_OPERATION = {
    "AUDIO_ENHANCE": "/enhance",
    "PERFORMANCE_TRANSFER": "/convert",
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
logger = logging.getLogger("abo.agent")


LOCAL_MODES = ("LOCAL_GPU", "LOCAL_CPU")
EXECUTION_MODES = (*LOCAL_MODES, "PROXY")


class Engine:
    """Un moteur, la version de modele ABO qu'il sert, et **ou il calcule**.

    Le mode d'execution appartient a la capacite et non a la machine
    (`ADR-016` § 1) : cet agent peut porter un `PROXY` vers une capacite louee
    a cote d'un `LOCAL_CPU` qui calcule sur place. C'est exactement ce que
    `ABO_ENGINES` decrivait deja sans savoir le dire — une entree par moteur,
    chacune avec sa propre adresse.
    """

    def __init__(
        self,
        engine_key: str,
        model_key: str,
        version_number: int,
        url: str,
        mode: str | None = None,
    ) -> None:
        self.engine_key = engine_key
        self.model_key = model_key
        self.version_number = version_number
        self.url = url.rstrip("/")
        self.serverless_endpoint: str | None = None
        self.serverless_route: str | None = None

        if self.url.startswith("vast+serverless://"):
            parsed = urlsplit(self.url)
            endpoint = unquote(parsed.netloc).strip()
            route = parsed.path
            if (
                not endpoint
                or not route.startswith("/")
                or route == "/"
                or parsed.query
                or parsed.fragment
            ):
                raise SystemExit(
                    "Moteur Vast mal forme : "
                    "vast+serverless://<endpoint>/<route> est attendu."
                )
            self.serverless_endpoint = endpoint
            self.serverless_route = route

        # **Le mode n'est pas libre de contredire l'adresse.** Une adresse
        # serverless est un `PROXY`, c'est un fait et non une preference ; et un
        # moteur qu'on joint dans son propre compose n'en est pas un. Declarer
        # l'inverse ferait entrer dans la ferme une machine dont la politique de
        # placement est fausse — un differe partirait sur une capacite facturee
        # a l'appel, ce que `specs/16` interdit.
        declared = (mode or "").strip().upper() or None
        if declared is not None and declared not in EXECUTION_MODES:
            raise SystemExit(
                f"Mode d'execution inconnu : « {mode} ». "
                "Attendu : " + ", ".join(EXECUTION_MODES)
            )
        if self.is_serverless:
            if declared is not None and declared != "PROXY":
                raise SystemExit(
                    f"« {engine_key} » vise une capacite louee et se declare "
                    f"{declared} : une adresse serverless est un PROXY."
                )
            self.mode = "PROXY"
        else:
            if declared == "PROXY":
                raise SystemExit(
                    f"« {engine_key} » se declare PROXY sur une adresse locale. "
                    "Un PROXY pilote une capacite externe."
                )
            # Sans precision, le moteur est local et sur carte : c'est ce que
            # toute la ferme sert aujourd'hui, et le placement ne distingue de
            # toute facon que « local » de « facture a l'appel ».
            self.mode = declared or "LOCAL_GPU"

    @property
    def is_serverless(self) -> bool:
        return self.serverless_endpoint is not None

    def declaration(self) -> dict:
        return {
            "engineKey": self.engine_key,
            "modelKey": self.model_key,
            "versionNumber": self.version_number,
            "executionMode": self.mode,
        }


def parse_engines(spec: str) -> list[Engine]:
    """`engineKey|modelKey|versionNumber|url[|mode]`, separes par des virgules.

    Un moteur mal decrit arrete l'agent au demarrage. Se declarer a moitie
    reviendrait a rejoindre la ferme en promettant une capacite qu'on ne sert
    pas, et l'erreur ne se verrait qu'au premier job d'un utilisateur.

    Le cinquieme champ est le **mode d'execution** de cette capacite-la
    (`ADR-016` § 1). Il est optionnel : une adresse serverless donne `PROXY`
    d'elle-meme, et un moteur local sans precision est `LOCAL_GPU` — ce que
    sert la ferme d'aujourd'hui. Le poser sert a nommer un `LOCAL_CPU`, qui est
    un niveau de service et non un GPU au rabais.
    """
    engines: list[Engine] = []
    for entry in (part.strip() for part in spec.split(",")):
        if not entry:
            continue
        fields = [field.strip() for field in entry.split("|")]
        if len(fields) not in (4, 5) or not all(fields):
            raise SystemExit(
                f"ABO_ENGINES mal forme : « {entry} ». "
                "Attendu : engineKey|modelKey|versionNumber|url[|mode]"
            )
        engine_key, model_key, version, url = fields[:4]
        mode = fields[4] if len(fields) == 5 else None
        if not version.isdigit():
            raise SystemExit(f"Numero de version invalide dans « {entry} ».")
        engines.append(Engine(engine_key, model_key, int(version), url, mode))

    if not engines:
        raise SystemExit("ABO_ENGINES est vide : cette machine n'a rien a servir.")
    return engines


@dataclass(frozen=True)
class VastEndpoint:
    """Le nom routable et le secret propre a un endpoint Vast."""

    name: str
    api_key: str


class VastServerlessRelay:
    """Transport Vast du relais, sans aucune decision d'ordonnancement ABO.

    L'agent n'arrive ici qu'apres avoir recu un bail normal du backend. Il
    hydrate alors les petits medias du job, demande un worker au routeur Vast,
    puis lui transmet une seule operation sans etat.
    """

    def __init__(self, client: httpx.Client, api_key: str = VAST_API_KEY) -> None:
        if not api_key:
            raise SystemExit("ABO_VAST_API_KEY est requis par un moteur serverless.")
        self._client = client
        self._api_key = api_key
        self._endpoints: dict[str, VastEndpoint] | None = None
        self._tls_client: httpx.Client | None = None

    @staticmethod
    def _auth(api_key: str) -> dict[str, str]:
        return {"Authorization": "Bearer " + api_key}

    def _load_endpoints(self) -> dict[str, VastEndpoint]:
        if self._endpoints is not None:
            return self._endpoints
        response = self._client.get(
            VAST_CONSOLE_URL + "/api/v0/endptjobs/",
            headers=self._auth(self._api_key),
            params={"client_id": "me", "api_key": self._api_key},
            timeout=BACKEND_TIMEOUT,
        )
        if response.status_code != 200:
            raise EngineError(
                f"Vast refuse la liste des endpoints ({response.status_code})."
            )
        try:
            rows = response.json().get("results", []) or []
        except (AttributeError, ValueError) as failure:
            raise EngineError("Vast a rendu une liste d'endpoints illisible.") from failure

        endpoints: dict[str, VastEndpoint] = {}
        for row in rows:
            config = row.get("config") or {}
            name = str(row.get("endpoint_name") or config.get("endpoint_name") or "")
            endpoint_key = row.get("api_key")
            if not name or not endpoint_key:
                continue
            endpoint = VastEndpoint(name=name, api_key=str(endpoint_key))
            endpoints[name] = endpoint
            if row.get("id") is not None:
                endpoints[str(row["id"])] = endpoint
        self._endpoints = endpoints
        return endpoints

    def endpoint(self, engine: Engine) -> VastEndpoint:
        endpoint = self._load_endpoints().get(engine.serverless_endpoint or "")
        if endpoint is None:
            raise EngineError(
                f"Endpoint Vast inconnu : {engine.serverless_endpoint or '?'}"
            )
        return endpoint

    def validate(self, engines: list[Engine]) -> None:
        """Refuse l'enrolement si une capacite annoncee n'existe pas chez Vast."""
        for engine in engines:
            endpoint = self.endpoint(engine)
            logger.info(
                "endpoint Vast pret : %s -> %s%s",
                engine.engine_key,
                endpoint.name,
                engine.serverless_route,
            )

    def _worker_client(self, worker_url: str) -> httpx.Client:
        if not worker_url.casefold().startswith("https://"):
            return self._client
        if self._tls_client is None:
            certificate = self._client.get(
                VAST_CONSOLE_URL + "/static/jvastai_root.cer",
                timeout=BACKEND_TIMEOUT,
            )
            certificate.raise_for_status()
            context = ssl.create_default_context()
            try:
                context.load_verify_locations(cadata=certificate.text)
            except ssl.SSLError as failure:
                raise EngineError("Certificat de worker Vast illisible.") from failure
            self._tls_client = httpx.Client(verify=context)
        return self._tls_client

    def post(self, engine: Engine, payload: dict) -> httpx.Response:
        endpoint = self.endpoint(engine)
        request_idx = 0
        deadline = time.monotonic() + VAST_ROUTE_TIMEOUT

        while True:
            response = self._client.post(
                VAST_SERVERLESS_URL + "/route/",
                headers=self._auth(endpoint.api_key),
                params={"api_key": endpoint.api_key},
                json={
                    "endpoint": endpoint.name,
                    "api_key": endpoint.api_key,
                    "cost": 100,
                    "request_idx": request_idx,
                    "replay_timeout": VAST_ROUTE_TIMEOUT,
                },
                timeout=BACKEND_TIMEOUT,
            )
            if response.status_code != 200:
                raise EngineError(
                    f"Routeur Vast indisponible ({response.status_code})."
                )
            try:
                route = response.json()
            except ValueError as failure:
                raise EngineError("Routeur Vast illisible.") from failure

            request_idx = int(
                route.get("request_idx") or route.get("reqnum") or request_idx
            )
            worker_url = route.get("url")
            if worker_url:
                break
            if time.monotonic() >= deadline:
                raise EngineError("Aucun worker Vast disponible avant l'expiration du bail.")
            time.sleep(VAST_ROUTE_POLL_SECONDS)

        client = self._worker_client(str(worker_url))
        return client.post(
            str(worker_url).rstrip("/") + (engine.serverless_route or ""),
            headers=self._auth(endpoint.api_key),
            params={"api_key": endpoint.api_key},
            json={"auth_data": route, "session_id": None, "payload": payload},
            timeout=ENGINE_TIMEOUT,
        )

    def close(self) -> None:
        if self._tls_client is not None:
            self._tls_client.close()


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
        """Va chercher un profil que cette machine n'a pas encore.

        Deux formes, et c'est le backend qui decide laquelle il sert selon la
        version annoncee a l'enrolement (`ABOB-137`). La forme cible ne rend
        qu'une **concession** : les 25 Mo viennent alors du stockage, sans
        traverser le plan de controle. C'est le seul transfert du projet ou
        quelqu'un attend devant l'ecran.

        L'empreinte est **verifiee ici**. Une concession designe un tiers : la
        suivre sans confronter ce qu'elle rend a ce qu'on demandait
        reviendrait a faire chanter au moteur une voix qu'on n'a pas choisie.
        """
        response = self._client.get(
            f"{self._base}/voices/{sha256}",
            headers=self._headers,
            timeout=BACKEND_TIMEOUT,
        )
        response.raise_for_status()
        corps = response.json()

        concession = corps.get("grant")
        if not concession:
            # Le backend sert encore l'ancienne forme. L'agent parle aux deux
            # le temps que le deploiement rattrape.
            return corps["voiceB64"]

        octets = self.fetch(concession)
        if hashlib.sha256(octets).hexdigest() != sha256:
            raise EngineError(
                "Le profil recu ne correspond pas a l'empreinte demandee."
            )
        # Le moteur, lui, parle JSON sur `127.0.0.1` : l'encodage revient, mais
        # sur une boucle locale ou il ne se paie pas.
        return base64.b64encode(octets).decode("ascii")

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

    def deposit_grant(
        self,
        job_id: str,
        attempt: int,
        kind: str,
        payload: bytes,
        content_type: str,
    ) -> dict | None:
        """Demande de quoi deposer ce qu'on vient de produire (`ADR-013`).

        On annonce ce qu'on a — nature, taille, empreinte — et le backend rend
        une concession qui **porte ces contraintes signees**. Le stockage les
        oppose ensuite lui-meme : d'autres octets, ou une autre longueur, sont
        refuses a l'ecriture.

        `None` quand la route n'existe pas : ce backend est anterieur a la
        decision, et l'appelant retombera sur ce qu'il sait faire. Un `404` est
        donc une reponse, pas une panne — et c'est le seul code qu'on traite
        ainsi, pour ne pas confondre « pas cette version » avec « refuse ».
        """
        response = self._post(
            f"/jobs/{job_id}/media/grant",
            {
                "attempt": attempt,
                "kind": kind,
                "sizeBytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "contentType": content_type,
            },
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

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

        **Une adresse absolue ne recoit aucun parametre.** Une URL presignee
        signe sa propre query : y ajouter `attempt` ou `kind` invaliderait la
        signature, et le stockage repondrait `403` sans rien expliquer. Ces
        deux valeurs ne servent qu'a notre API, qui frappe l'identite en
        recevant ; quand un tiers ecrit, l'identite est deja frappee et voyage
        dans la concession.
        """
        url = grant["url"]
        headers = dict(grant.get("headers") or {})
        direct = not url.startswith("/")
        if not direct:
            url = BACKEND_URL + url
            headers.update(self._headers)
        headers.setdefault("Content-Type", content_type)

        response = self._client.request(
            grant.get("method", "POST"),
            url,
            params=None if direct else {"attempt": attempt, "kind": kind},
            content=payload,
            headers=headers,
            timeout=BACKEND_TIMEOUT,
        )
        response.raise_for_status()
        if not direct:
            return response.json()

        # Le stockage ne rend pas de JSON : la reference est celle que la
        # concession portait deja, et l'empreinte se calcule ici. Le backend la
        # reverifiera de son cote — il ne croit pas la machine sur parole.
        return {
            "mediaId": grant["mediaId"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "sizeBytes": len(payload),
        }

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


def _post_engine(
    client: httpx.Client,
    engine: Engine,
    route: str,
    body: dict,
    relay: VastServerlessRelay | None,
) -> httpx.Response:
    if engine.is_serverless:
        if relay is None:
            raise EngineError("Transport Vast absent pour ce moteur serverless.")
        return relay.post(engine, body)
    return client.post(engine.url + route, json=body, timeout=ENGINE_TIMEOUT)


def synthesize(
    client: httpx.Client,
    engine: Engine,
    job_input: dict,
    backend: "Backend",
    config: dict,
    relay: VastServerlessRelay | None,
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

    response = _post_engine(client, engine, "/synthesize", body, relay)
    if response.status_code == 409 and body["voice_sha256"]:
        # Le chemin lent, et il doit le rester : une fois par machine et par
        # voix. Le profil ne devient pas durable ici, il alimente un cache.
        logger.info("profil absent du cache, recuperation : %s", body["voice_sha256"][:12])
        body["voice_b64"] = backend.voice(body["voice_sha256"])
        response = _post_engine(client, engine, "/synthesize", body, relay)

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
    relay: VastServerlessRelay | None,
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
        engine,
        "/enroll",
        {
            "reference_b64": reference,
            "voice_name": job_input.get("voiceName", "voix"),
            "language": job_input.get("language", "French"),
            "reference_text": job_input.get("referenceText", "") or "",
        },
        relay,
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
    relay: VastServerlessRelay | None,
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
        engine,
        "/design",
        {
            "description": description,
            "text": job_input.get("text", ""),
            "language": job_input.get("language", "French"),
        },
        relay,
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
    relay: VastServerlessRelay | None,
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
        client,
        engine,
        "/enhance",
        {"audio_b64": audio, "config": config},
        relay,
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
    relay: VastServerlessRelay | None,
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
        engine,
        "/convert",
        {"audio_b64": performance, "reference_b64": reference, "config": config},
        relay,
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
    client: httpx.Client,
    engines: dict[str, Engine],
    assignment: dict,
    backend: "Backend",
    relay: VastServerlessRelay | None = None,
) -> dict:
    engine = engines.get(assignment["engineKey"])
    if engine is None:
        # Le backend a attribue un travail pour un moteur que cette machine ne
        # porte pas : sa declaration et sa realite ont diverge.
        raise EngineError(f"Moteur non porte : {assignment['engineKey']}")

    operation = assignment["operation"]
    if engine.is_serverless and operation not in STATELESS_OPERATIONS:
        # Cette borne est locale et volontairement redondante avec le registre.
        # Une route mal configuree ne doit jamais faire transiter un profil de
        # voix durable par un worker serverless non epingle.
        raise EngineError(f"Operation avec etat interdite au relais Vast : {operation}")
    expected_route = SERVERLESS_ROUTE_BY_OPERATION.get(operation)
    if engine.is_serverless and engine.serverless_route != expected_route:
        raise EngineError(
            f"Route Vast incoherente pour {operation} : {engine.serverless_route}"
        )

    handler = HANDLERS.get(operation)
    if handler is None:
        raise EngineError(f"Operation non servie par cet agent : {assignment['operation']}")

    # Le reglage vient de la route. Un backend plus ancien n'en envoie pas :
    # l'absence vaut « rien de particulier », pas une erreur.
    config = assignment.get("engineConfig") or {}

    started = time.monotonic()
    payload = handler(
        client,
        engine,
        assignment.get("input") or {},
        backend,
        config,
        relay,
    )
    payload["metrics"]["computeMs"] = int((time.monotonic() - started) * 1000)
    return _deposit_output(payload, assignment, backend)


def _deposit_output(payload: dict, assignment: dict, backend: "Backend") -> dict:
    """Depose les octets produits, et ne garde qu'une reference a rendre.

    **La concession se demande quand les octets existent** (`ADR-013`). C'est ce
    qui permet au backend de ranger un `.qvoice` dans le durable et une prise
    dans le travail : au moment du bail, personne ne sait encore lequel des deux
    ce travail produira. Une seule concession pour les deux natures ferait
    atterrir un profil de voix dans un stockage temporaire.

    Trois formes coexistent, et l'agent choisit d'apres ce que le backend lui
    donne, jamais d'apres sa propre version :

    - le bail porte une concession — un backend d'avant `ADR-013`, on s'en sert ;
    - il n'en porte pas et la route de concession repond — la forme cible ;
    - il n'en porte pas et la route n'existe pas — un backend d'avant
      `ADR-010`, et le resultat repart en base64 comme autrefois.

    Ce qu'un `artifactB64` porte est un profil de voix de 25 Mo : c'est le plus
    gros objet du systeme, et celui pour lequel `ADR-010` a ete ecrit.
    """
    attempt = assignment["attempt"]
    du_bail = assignment.get("deposit")

    for cle_b64, cle_media, content_type, kind in (
        ("audioB64", "outputMediaId", "audio/wav", "output"),
        ("artifactB64", "artifactMediaId", "application/octet-stream", "artifact"),
    ):
        encode = payload.pop(cle_b64, None)
        if not encode:
            continue
        octets = base64.b64decode(encode)

        concession = du_bail or backend.deposit_grant(
            assignment["jobId"], attempt, kind, octets, content_type
        )
        if concession is None:
            # Ce backend ne sait recevoir aucun depot : on lui rend les octets
            # sous la forme qu'il comprend plutot que de perdre le travail.
            payload[cle_b64] = encode
            continue

        depose = backend.deposit(concession, attempt, octets, content_type, kind)
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

    with ExitStack() as stack:
        client = stack.enter_context(httpx.Client())
        backend = Backend(client)
        serverless_engines = [engine for engine in engines if engine.is_serverless]
        local_engines = [engine for engine in engines if not engine.is_serverless]
        relay = None
        if serverless_engines:
            relay = VastServerlessRelay(client)
            stack.callback(relay.close)
            try:
                relay.validate(serverless_engines)
            except (EngineError, httpx.HTTPError) as failure:
                raise SystemExit(
                    f"Capacite Vast serverless invalide : {failure}"
                ) from failure
        # Les moteurs d'abord, la ferme ensuite. L'ordre est le sujet
        # d'`ABOB-128` : declarer une capacite avant de l'avoir est une panne
        # qu'on fait decouvrir a un utilisateur.
        wait_for_engines(client, local_engines)
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
                        wait_for_engines(client, local_engines)
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
                    payload = execute(client, by_key, assignment, backend, relay)
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
