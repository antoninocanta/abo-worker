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
import hmac
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
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, unquote, urlsplit

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
#   0.8.0  **le conteneur embarque son noeud de maillage** (`ADR-015` § 1,
#          `ABOB-156`). Il refuse de lancer l'agent sans adresse de maillage :
#          une machine hors maillage n'est pas un worker degrade, elle n'est pas
#          un worker. Exige `/dev/net/tun`, `NET_ADMIN` et `NET_RAW`, declares
#          dans le compose. Le numero le dit parce qu'un operateur qui lit
#          `agent_version` dans la console doit savoir si cette machine est sur
#          le maillage ou pas.
#   0.9.0  **la sortie ne traverse plus le porteur** (`ADR-016` § 4,
#          `ABOB-173`). Deux appels dans une session Vast, qui epingle un
#          conteneur : la route du moteur avec `defer_output`, puis `/upload`
#          avec une concession signee sur la taille et l'empreinte que le moteur
#          a annoncees. Aucun octet, aucun rappel vers ABO, aucun secret ABO
#          chez Vast. Repli complet sur le base64 quand la concession n'est pas
#          confiable — un chemin qui echouerait en silence serait pire que son
#          absence.
#   0.10.0 la machine **signe elle-meme** ses depots (`ADR-017`). Elle demande un
#          jeu de credentials R2 temporaires bornes a `workers/<id>/`, le garde
#          en memoire, le renouvelle avant echeance, et signe les concessions
#          qu'elle confie a une capacite louee. Plus aucun aller-retour de plan
#          de controle par media. La version compte : le bail ne porte de creneau
#          de sortie qu'a partir d'ici.
#   0.11.0 l'URL de validation du maillage **remonte a la console** au lieu
#          d'attendre dans un journal (`ABOB-157`). L'entrypoint l'annonce, puis
#          annonce l'issue — l'adresse obtenue et le compte qui a valide, ou le
#          motif de l'echec. C'est le seul appel que la machine passe hors du
#          maillage, et il va donc a la surface **publique** : `ABO_BACKEND_URL`
#          designe desormais l'adresse de maillage du backend, et lui seul.
AGENT_VERSION = "0.11.0"

# **L'adresse de maillage du backend** depuis `ABOB-157` — la surface machine
# n'est plus servie par le tunnel public. Le defaut local reste ce qu'il etait :
# il ne sert qu'a un agent lance a la main contre un backend de developpement.
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
# Duree de vie d'une session Vast, **declaree et pas subie**. Chaque requete la
# rajoute a l'expiration cote serveur, donc elle n'a pas a couvrir tout le
# travail : seulement le plus long silence entre deux appels, ici l'aller-retour
# de concession sur le maillage. Le defaut du client Vast est de 60 s ; on prend
# large parce qu'une session qui expire au milieu perdrait le fichier, et que
# quelques secondes de location valent moins qu'un chapitre a recalculer.
VAST_SESSION_LIFETIME = float(os.getenv("ABO_VAST_SESSION_LIFETIME", "300"))

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

    def _route(self, endpoint: VastEndpoint) -> tuple[str, dict]:
        """Demande au routeur Vast une instance libre, et attend s'il n'y en a pas.

        Rendu a part parce que **deux chemins en ont besoin et un seul doit
        recommencer** : un appel simple route a chaque fois, une session route
        une seule fois puis reste collee a l'instance qu'elle a obtenue.
        """
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
                return str(worker_url), route
            if time.monotonic() >= deadline:
                raise EngineError("Aucun worker Vast disponible avant l'expiration du bail.")
            time.sleep(VAST_ROUTE_POLL_SECONDS)

    def _send(
        self,
        endpoint: VastEndpoint,
        worker_url: str,
        route: str,
        auth_data: dict,
        payload: dict,
        session_id: str | None = None,
    ) -> httpx.Response:
        client = self._worker_client(worker_url)
        return client.post(
            worker_url.rstrip("/") + route,
            headers=self._auth(endpoint.api_key),
            params={"api_key": endpoint.api_key},
            json={"auth_data": auth_data, "session_id": session_id, "payload": payload},
            timeout=ENGINE_TIMEOUT,
        )

    def post(self, engine: Engine, payload: dict) -> httpx.Response:
        endpoint = self.endpoint(engine)
        worker_url, route = self._route(endpoint)
        return self._send(
            endpoint, worker_url, engine.serverless_route or "", route, payload
        )

    def open_session(self, engine: Engine, lifetime: float) -> "VastSession":
        """Ouvre une session, et **s'y colle**.

        C'est ce qui rend la sortie differee possible : le client Vast route une
        session une seule fois, et le sien porte le commentaire qui tranche —
        `# Session is bound to this worker - can't re-route`. Les deux appels
        atteignent donc le meme conteneur, donc le meme systeme de fichiers.

        `lifetime` est **declare et pas subi**. Chaque requete le rajoute a
        l'expiration (`session.expiration += session.lifetime` cote serveur),
        donc il n'a pas a couvrir tout le travail — seulement le plus long des
        silences entre deux appels, ici l'aller-retour de concession sur le
        maillage. Le defaut du client Vast est de 60 s, et un defaut n'est pas
        une decision : une session qui expire au milieu perdrait le fichier.
        """
        endpoint = self.endpoint(engine)
        worker_url, route = self._route(endpoint)
        response = self._send(
            endpoint, worker_url, "/session/create", route, {"lifetime": lifetime}
        )
        if response.status_code != 200:
            raise EngineError(
                f"Session Vast refusee ({response.status_code}) {response.text[:200]}"
            )
        try:
            session_id = response.json()["payload"]["session_id"]
        except (ValueError, KeyError, TypeError):
            # Certaines versions rendent la charge a plat. On accepte les deux
            # plutot que d'echouer sur une enveloppe.
            try:
                session_id = response.json()["session_id"]
            except (ValueError, KeyError, TypeError) as failure:
                raise EngineError("Session Vast sans identifiant.") from failure
        return VastSession(
            relay=self,
            endpoint=endpoint,
            session_id=str(session_id),
            worker_url=worker_url,
            auth_data=route,
        )

    def close(self) -> None:
        if self._tls_client is not None:
            self._tls_client.close()


@dataclass
class VastSession:
    """Une session Vast, collee a une instance.

    Ce qu'elle garantit n'est pas de notre fait : c'est le client Vast qui
    refuse de rerouter une session, et le serveur qui rend `410` sur une session
    inconnue. Ce que **nous** garantissons est de ne jamais redemander de route
    tant qu'elle vit — sans quoi on perdrait l'affinite en croyant l'avoir.
    """

    relay: "VastServerlessRelay"
    endpoint: VastEndpoint
    session_id: str
    worker_url: str
    auth_data: dict

    def post(self, route: str, payload: dict) -> httpx.Response:
        response = self.relay._send(
            self.endpoint,
            self.worker_url,
            route,
            self.auth_data,
            payload,
            session_id=self.session_id,
        )
        if response.status_code == 410:
            # Le worker qui portait cette session a disparu. Ce n'est pas une
            # panne a masquer : le job repart ailleurs, et la reprise sait deja
            # le faire (`specs/17`).
            raise EngineError("La session Vast a ete fermee par le worker.")
        return response

    def close(self) -> None:
        """Termine la session. Un echec ici ne fait pas echouer un travail fini.

        Le ramasse-miettes du worker ferme les sessions expirees tout seul
        (`__session_gc_loop`), donc au pire on a perdu quelques secondes de
        location — jamais un resultat deja depose.
        """
        try:
            self.relay._send(
                self.endpoint,
                self.worker_url,
                "/session/end",
                self.auth_data,
                {"session_id": self.session_id},
                session_id=self.session_id,
            )
        except (httpx.HTTPError, EngineError) as failure:
            logger.warning("fermeture de session Vast echouee : %s", failure)


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
        # Les credentials R2 de cette machine, gardes **en memoire seulement**.
        # Rien sur disque : un secret de douze heures ecrit quelque part est un
        # secret a effacer, et un conteneur qui redemarre en redemande un.
        self._vault: Vault | None = None

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
        return self.deposit_grant_for(
            job_id,
            attempt,
            kind,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            content_type,
        )

    def vault(self) -> "Vault | None":
        """Les credentials R2 de cette machine, frappes ou renouveles au besoin.

        **C'est ici que le renouvellement vit**, et nulle part ailleurs : le
        demander a chaque usage avec une marge est plus simple qu'une horloge de
        fond, et ca ne coute un appel qu'une ou deux fois par jour.

        `None` quand ce deploiement ne frappe pas de credentials — l'appelant
        retombe alors sur la concession par media (`ADR-016` § 4), qui reste
        correcte et coute un aller-retour. Un `503` est donc une **reponse**,
        pas une panne, exactement comme le `404` de `deposit_grant`.
        """
        if self._vault is not None and self._vault.usable:
            return self._vault

        response = self._post("/credentials", {})
        if response.status_code in (404, 503):
            logger.info("pas de credentials R2 servis, repli sur la concession par media")
            self._vault = None
            return None
        if response.status_code == 409:
            # Drainee ou revoquee : elle n'a plus a ecrire, et le backend a
            # raison de le refuser. Le travail en cours se rendra par l'autre
            # chemin plutot que d'echouer.
            logger.warning("credentials refuses : %s", response.text[:200])
            self._vault = None
            return None
        response.raise_for_status()

        self._vault = Vault.depuis(response.json())
        logger.info(
            "credentials R2 obtenus, prefixe %s, echeance %s",
            self._vault.prefix,
            self._vault.expires_at.isoformat(),
        )
        return self._vault

    def deposit_grant_for(
        self,
        job_id: str,
        attempt: int,
        kind: str,
        size_bytes: int,
        sha256: str,
        content_type: str,
        fallback_reason: str = "",
    ) -> dict | None:
        """La meme concession, **sans tenir les octets** (`ADR-016` § 4).

        Quand le calcul a eu lieu sur une capacite louee, l'agent n'a jamais la
        sortie : le moteur la garde le temps de la session et n'annonce que sa
        taille et son empreinte. Les contraintes signees sont donc exactement
        les memes — c'est ce qui permet a `ADR-011` et `ADR-013` de tenir tels
        quels alors qu'aucun octet ne traverse cette machine.
        """
        response = self._post(
            f"/jobs/{job_id}/media/grant",
            {
                "attempt": attempt,
                "kind": kind,
                "sizeBytes": size_bytes,
                "sha256": sha256,
                "contentType": content_type,
                # Pourquoi on passe par ici alors qu'on sait signer. Le backend
                # le compte : sans motif, le repli pourrait redevenir la route
                # normale sans que personne le voie (`ADR-017`).
                "fallbackReason": fallback_reason,
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
    sur une boucle locale, ou il ne se paie pas.

    Cette derniere phrase etait fausse d'un cas, et c'est `ADR-016` § 4 qui l'a
    releve : quand le moteur est **de l'autre cote d'Internet**, le meme tiers se
    paie sur la ligne montante du porteur. Voir `audio_fields`, qui choisit la
    forme selon la distance ; celle-ci reste la forme par octets.
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


def _passable(grant: dict | None) -> str | None:
    """L'adresse d'une concession qu'on peut confier a un tiers, ou rien.

    Deux refus, et les deux comptent :

    - une adresse **relative** designe l'API d'ABO, et `Backend.fetch` y ajoute
      le secret de cette machine. La passer a un moteur loue **lui donnerait ce
      secret** — la faute exacte que `ADR-009` § 5 interdit ;
    - une concession qui porte des **en-tetes** ne se suit pas sans eux, et un
      moteur n'en envoie aucun a dessein : un `x-amz-*` non signe fait refuser
      toute la requete par R2. Mieux vaut hydrater que fabriquer un `403` que
      rien n'expliquera.

    Dans les deux cas on retombe sur le base64, qui marche toujours. Une
    optimisation qui echoue en silence serait pire que son absence.
    """
    if not grant:
        return None
    url = str(grant.get("url") or "")
    if not url.startswith("https://") or grant.get("headers"):
        return None
    if str(grant.get("method", "GET")).upper() != "GET":
        return None
    return url


def audio_fields(
    job_input: dict, name: str, backend: "Backend", engine: Engine
) -> dict:
    """Le fragment de corps qui porte cette entree — octets, ou reference.

    **C'est la distance qui decide** (`ADR-016` § 4). Un moteur dans le meme
    compose recoit les octets : ca ne coute rien sur un reseau Docker, et il
    reste sans acces sortant. Un moteur de l'autre cote d'Internet recoit une
    **concession signee** et va lire lui-meme, parce qu'un `PROXY` est un pilote
    et non un relais — relayer lui ferait porter 13,4 Mo par minute d'audio sur
    une ligne domestique.

    L'empreinte part avec la reference : c'est l'agent qui verifiait que le
    stockage avait rendu ce qui etait annonce, et il ne peut plus le faire s'il
    ne lit pas. Le moteur reprend ce controle plutot que de le perdre.
    """
    if engine.is_serverless:
        reference = job_input.get(name) or {}
        url = _passable(reference.get("grant"))
        if url:
            return {
                name + "_url": url,
                name + "_sha256": str(reference.get("sha256") or ""),
            }

    octets = audio_from(job_input, name, backend)
    return {name + "_b64": octets} if octets else {}


# --- Signer ses propres depots (`ADR-017`) -----------------------------------
#
# **Ce signeur est un doublon de celui du backend**, et il faut le dire : les
# deux depots n'ont aucun paquet commun, et l'agent n'a que `httpx`. Le risque
# est qu'ils divergent ; ce qui le borne est que SigV4 ne bouge pas, et que R2
# refuse tout ce qui s'en ecarte — une divergence rend `403`, jamais un silence.
ALGORITHME = "AWS4-HMAC-SHA256"
CHARGE_NON_SIGNEE = "UNSIGNED-PAYLOAD"
# Un caractere non reserve dans une query s'encode ; la barre oblique aussi,
# contrairement a un chemin. Les confondre coute une signature fausse et un
# `403` que rien n'explique — piege deja paye cote backend.
NON_RESERVE_QUERY = "-._~"
NON_RESERVE_CHEMIN = "-._~/"
# On redemande un jeu quand il reste moins que ca : signer avec des credentials
# qui expirent pendant le televersement rendrait un `403` au milieu d'un depot.
MARGE_RENOUVELLEMENT = timedelta(minutes=20)


def _hmac(cle: bytes, message: str) -> bytes:
    return hmac.new(cle, message.encode("utf-8"), hashlib.sha256).digest()


@dataclass
class Vault:
    """Les credentials R2 de cette machine, et de quoi signer avec.

    Bornes a `workers/<worker_id>/` et a quelques heures. Ils ne quittent jamais
    cette machine : ce qui part vers une capacite louee est une **URL signee**,
    un objet, un verbe, une duree courte.
    """

    access_key_id: str
    secret_access_key: str
    session_token: str
    prefix: str
    expires_at: datetime
    bucket: str
    endpoint_url: str
    region: str

    @classmethod
    def depuis(cls, corps: dict) -> "Vault":
        return cls(
            access_key_id=corps["accessKeyId"],
            secret_access_key=corps["secretAccessKey"],
            session_token=corps["sessionToken"],
            prefix=corps["prefix"],
            expires_at=datetime.fromisoformat(corps["expiresAt"]),
            bucket=corps["bucket"],
            endpoint_url=corps["endpointUrl"].rstrip("/"),
            region=corps.get("region") or "auto",
        )

    @property
    def usable(self) -> bool:
        return datetime.now(UTC) + MARGE_RENOUVELLEMENT < self.expires_at

    def _cle_de_signature(self, jour: str) -> bytes:
        cle = _hmac(("AWS4" + self.secret_access_key).encode("utf-8"), jour)
        cle = _hmac(cle, self.region)
        cle = _hmac(cle, "s3")
        return _hmac(cle, "aws4_request")

    def presign_put(self, object_key: str, size_bytes: int, sha256: str, ttl: int = 900):
        """Une concession d'ecriture pour **cet objet-la**, et rien d'autre.

        La taille et l'empreinte sont **signees**, donc opposees par R2 :
        d'autres octets ou une autre longueur sont refuses a l'ecriture
        (`ADR-011`, et `400 BadDigest` mesure). C'est ce qui permet de confier
        cette URL a une capacite louee sans lui confier quoi que ce soit
        d'autre.

        L'echeance est **bornee par celle du jeu**. Une URL signee plus
        longtemps que les credentials qui la signent promettrait une duree que
        personne ne tiendrait.
        """
        if not object_key.startswith(self.prefix):
            # Se le refuser ici plutot que de laisser R2 rendre `403` : le
            # message serait « Access Denied », et personne ne verrait que la
            # cle etait hors du prefixe de cette machine.
            raise EngineError(
                f"Cle hors du prefixe de cette machine : {object_key} (attendu {self.prefix}…)"
            )

        restant = int((self.expires_at - datetime.now(UTC)).total_seconds())
        expire_dans = max(60, min(ttl, restant))

        maintenant = datetime.now(UTC)
        horodate = maintenant.strftime("%Y%m%dT%H%M%SZ")
        jour = maintenant.strftime("%Y%m%d")
        portee = f"{jour}/{self.region}/s3/aws4_request"

        hote = urlsplit(self.endpoint_url).netloc
        chemin = "/" + self.bucket + "/" + quote(object_key, safe=NON_RESERVE_CHEMIN)

        signables = {
            "host": hote,
            "content-length": str(size_bytes),
            "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(sha256)).decode("ascii"),
        }
        liste_signee = ";".join(sorted(signables))
        entetes_canoniques = "".join(f"{nom}:{signables[nom]}\n" for nom in sorted(signables))

        query = {
            "X-Amz-Algorithm": ALGORITHME,
            "X-Amz-Credential": f"{self.access_key_id}/{portee}",
            "X-Amz-Date": horodate,
            "X-Amz-Expires": str(expire_dans),
            "X-Amz-Security-Token": self.session_token,
            "X-Amz-SignedHeaders": liste_signee,
        }
        query_canonique = "&".join(
            f"{quote(nom, safe=NON_RESERVE_QUERY)}={quote(valeur, safe=NON_RESERVE_QUERY)}"
            for nom, valeur in sorted(query.items())
        )
        # Un `join` et pas une f-string, malgre ce que suggere le linter : cette
        # forme doit se lire **exactement comme celle du backend**, parce que
        # les deux signent la meme chose et qu'une divergence de lecture est le
        # premier pas vers une divergence de comportement. Et le `\n` final
        # d'`entetes_canoniques` produit la ligne vide que SigV4 exige — visible
        # ici, invisible dans une f-string.
        requete_canonique = "\n".join(  # noqa: FLY002
            ["PUT", chemin, query_canonique, entetes_canoniques, liste_signee, CHARGE_NON_SIGNEE]
        )
        a_signer = "\n".join(
            [
                ALGORITHME,
                horodate,
                portee,
                hashlib.sha256(requete_canonique.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            self._cle_de_signature(jour), a_signer.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        url = f"{self.endpoint_url}{chemin}?{query_canonique}&X-Amz-Signature={signature}"
        # `host` n'est pas rendu : le porteur le pose lui-meme.
        joints = {nom: valeur for nom, valeur in signables.items() if nom != "host"}
        return url, joints


def _rendu(response: httpx.Response) -> dict:
    """La charge d'une reponse de moteur, enveloppee ou non.

    Le PyWorker rend parfois `{"payload": {...}}`, parfois la charge a plat
    selon la version. Accepter les deux vaut mieux qu'echouer sur une enveloppe
    — le moteur, lui, a fait son travail.
    """
    try:
        corps = response.json()
    except ValueError as failure:
        raise EngineError("Le moteur a rendu une reponse illisible.") from failure
    if isinstance(corps, dict) and isinstance(corps.get("payload"), dict):
        return corps["payload"]
    return corps if isinstance(corps, dict) else {}


# --- Le repli, borne et jamais silencieux (`ADR-017`) ------------------------
#
# Il existe pour ne pas perdre de travail, et c'est la seule raison. **Il ne doit
# jamais devenir la route normale sans qu'on le voie** : chaque cause est nommee,
# criee en avertissement, et transmise au backend qui la compte. Une cause qu'on
# ne saurait pas nommer n'en est pas une — on ne replie pas dessus.
REPLI_SANS_CREDENTIALS = "no-credentials"
REPLI_SANS_CRENEAU = "no-output-slot"
REPLI_CONCESSION_RELATIVE = "relative-grant"
REPLI_SANS_CONCESSION = "no-grant"
REPLI_MOTEUR_ANCIEN = "engine-without-deferred-output"


def _crie_le_repli(motif: str, job_id: str) -> None:
    """Un avertissement, jamais une info.

    Le niveau **est** la decision : un `info` se noie dans un journal de
    production, et le jour ou le repli redeviendrait la route normale personne
    ne le verrait avant une facture de bande passante.
    """
    logger.warning("repli sur la concession par media : %s (job=%s)", motif, job_id)


def _concession_de_depot(
    backend: "Backend",
    creneau: dict | None,
    job_id: str,
    attempt: int,
    kind: str,
    taille: int,
    empreinte: str,
    content_type: str,
) -> dict | None:
    """De quoi deposer, signe **ici** quand c'est possible (`ADR-017`).

    Deux chemins, et le premier est celui qui ne coute aucun aller-retour :

    - le bail portait un **creneau** — une ligne de media et sa cle, deja
      frappees sous le prefixe de cette machine — et on a des credentials : on
      signe soi-meme. Le plan de controle n'est pas dans le chemin d'ecriture ;
    - sinon on demande au backend (`ADR-016` § 4), qui reste correct.

    Le repli n'est pas une tiedeur : un deploiement sans jeton d'API Cloudflare,
    une machine drainee, un agent plus recent que son backend — les trois
    arrivent, et perdre le travail pour l'un d'eux serait pire que l'appel
    qu'on economise.
    """
    if creneau:
        vault = backend.vault()
        if vault is not None:
            url, entetes = vault.presign_put(str(creneau["objectKey"]), taille, empreinte)
            return {"url": url, "headers": entetes, "mediaId": creneau["mediaId"]}
        motif = REPLI_SANS_CREDENTIALS
    else:
        motif = REPLI_SANS_CRENEAU

    _crie_le_repli(motif, job_id)
    return backend.deposit_grant_for(
        job_id, attempt, kind, taille, empreinte, content_type, fallback_reason=motif
    )


def deposited_elsewhere(
    engine: Engine,
    route: str,
    body: dict,
    relay: VastServerlessRelay | None,
    backend: "Backend",
    job_id: str,
    attempt: int,
    content_type: str = "audio/wav",
    kind: str = "output",
    creneau: dict | None = None,
) -> dict | None:
    """Fait calculer **et deposer** par la capacite louee, sans porter un octet.

    Deux appels dans une session Vast, qui epingle un conteneur (`ADR-016` § 4) :

    1. la route du moteur avec `defer_output` — il calcule, garde le fichier et
       rend `output_id`, taille et empreinte ;
    2. le backend signe une concession sur ces valeurs **exactes**, sur le
       maillage. `ADR-011` et `ADR-013` tiennent donc tels quels ;
    3. `/upload` dans la **meme** session : le moteur suit la concession et
       depose en direct sur R2, puis oublie son temporaire.

    Rend `None` quand ce chemin n'est pas praticable, et l'appelant retombe
    alors sur le base64 — qui marche toujours. Deux cas, et aucun ne doit
    echouer en silence :

    - le backend ne sait pas delivrer de concession (version anterieure) ;
    - la concession n'est **pas une adresse absolue**. Une adresse relative
      designe l'API d'ABO et exige le secret de cette machine : la donner a une
      capacite louee reviendrait a le lui offrir, ce qu'`ADR-009` § 5 interdit.
    """
    if relay is None or not engine.is_serverless:
        return None

    session = relay.open_session(engine, lifetime=VAST_SESSION_LIFETIME)
    try:
        response = session.post(route, {**body, "defer_output": True})
        if response.status_code == 501:
            raise EngineError(f"{engine.engine_key} ne porte pas {route}")
        if response.status_code != 200:
            raise EngineError(f"{response.status_code} {response.text[:300]}")

        rendu = _rendu(response)
        output_id = str(rendu.get("output_id") or "")
        taille = int(rendu.get("size_bytes") or 0)
        empreinte = str(rendu.get("sha256") or "")
        if not (output_id and taille and empreinte):
            # Le moteur a repondu la forme directe : il est anterieur a
            # `ADR-016`. On le dit plutot que de deviner, et l'appelant rejoue
            # en base64 avec l'image qui tourne.
            _crie_le_repli(REPLI_MOTEUR_ANCIEN, job_id)
            return None

        concession = _concession_de_depot(
            backend, creneau, job_id, attempt, kind, taille, empreinte, content_type
        )
        if concession is None:
            _crie_le_repli(REPLI_SANS_CONCESSION, job_id)
            return None
        adresse = str(concession.get("url") or "")
        if not adresse.startswith("https://"):
            _crie_le_repli(REPLI_CONCESSION_RELATIVE, job_id)
            return None

        depot = session.post(
            "/upload",
            {
                "output_id": output_id,
                "put_url": adresse,
                # Les en-tetes portent la taille et l'empreinte signees, et
                # partent tels quels : un `x-amz-*` en trop ou en moins fait
                # refuser toute la requete.
                "headers": dict(concession.get("headers") or {}),
            },
        )
        if depot.status_code != 200:
            raise EngineError(f"depot refuse : {depot.status_code} {depot.text[:200]}")

        return {
            "mediaId": concession["mediaId"],
            "sha256": empreinte,
            "sizeBytes": taille,
            "metrics": {
                "sizeBytes": taille,
                "enginePath": rendu.get("engine", "unknown"),
            },
            "format": rendu.get("format", "wav"),
        }
    finally:
        session.close()


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
    assignment: dict,
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
    assignment: dict,
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
    assignment: dict,
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
    assignment: dict,
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
    fields = audio_fields(job_input, "audio", backend, engine)
    if not fields:
        raise EngineError("Aucun audio a nettoyer dans ce travail.")

    corps = {**fields, "config": config}
    ailleurs = deposited_elsewhere(
        engine, "/enhance", corps, relay, backend,
        assignment["jobId"], assignment["attempt"],
        creneau=assignment.get("outputSlot"),
    )
    if ailleurs is not None:
        # La sortie est deja sur R2 : aucun octet n'a traverse cette machine.
        return {
            "outputMediaId": ailleurs["mediaId"],
            "outputSha256": ailleurs["sha256"],
            "format": ailleurs["format"],
            "metrics": ailleurs["metrics"],
        }

    response = _post_engine(
        client,
        engine,
        "/enhance",
        corps,
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
    assignment: dict,
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
    performance = audio_fields(job_input, "audio", backend, engine)
    reference = audio_fields(job_input, "reference", backend, engine)
    if not performance:
        raise EngineError("Aucune performance dans ce travail.")
    if not reference:
        raise EngineError("Aucune voix de reference dans ce travail.")

    corps = {**performance, **reference, "config": config}
    ailleurs = deposited_elsewhere(
        engine, "/convert", corps, relay, backend,
        assignment["jobId"], assignment["attempt"],
        creneau=assignment.get("outputSlot"),
    )
    if ailleurs is not None:
        # L'operation qui gagne le plus : deux entrees pour une sortie, donc
        # deux tiers du volume qui ne traversent plus la ligne du porteur.
        return {
            "outputMediaId": ailleurs["mediaId"],
            "outputSha256": ailleurs["sha256"],
            "format": ailleurs["format"],
            "metrics": ailleurs["metrics"],
        }

    response = _post_engine(
        client,
        engine,
        "/convert",
        corps,
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
        assignment,
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
