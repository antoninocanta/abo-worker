import base64
import hashlib
import json

import httpx
import pytest

import agent


def _engine(operation="enhance"):
    return agent.Engine(
        "clearervoice",
        "audio.clearervoice",
        1,
        f"vast+serverless://Clearer%20Voice/{operation}",
    )


def _reponse(corps: dict) -> httpx.Response:
    return httpx.Response(
        200, request=httpx.Request("POST", "http://worker.test/x"), json=corps
    )


SORTIE = b"clean"
SHA_SORTIE = hashlib.sha256(SORTIE).hexdigest()
RENDU_DIFFERE = {
    "output_id": "f" * 32,
    "sha256": SHA_SORTIE,
    "size_bytes": len(SORTIE),
    "format": "wav",
    "engine": "vast-test",
}
CONCESSION = {
    "mediaId": "media-1",
    "url": "https://r2.test/travail/sortie?sig=xyz",
    "headers": {"content-length": str(len(SORTIE)), "x-amz-checksum-sha256": "Zm9v"},
}


def test_parse_un_moteur_serverless_sans_relacher_la_version_abo():
    engine = _engine()

    assert engine.declaration() == {
        "engineKey": "clearervoice",
        "modelKey": "audio.clearervoice",
        "versionNumber": 1,
        # Deduit de l'adresse, pas declare : une capacite serverless **est** un
        # `PROXY` (`ADR-016` § 1).
        "executionMode": "PROXY",
    }
    assert engine.serverless_endpoint == "Clearer Voice"
    assert engine.serverless_route == "/enhance"


def test_un_moteur_serverless_exige_un_endpoint_et_une_route():
    with pytest.raises(SystemExit, match="mal forme"):
        agent.Engine("clearervoice", "audio.clearervoice", 1, "vast+serverless://endpoint")


def test_une_machine_melange_les_modes_et_chaque_capacite_dit_le_sien():
    """L'hybride que `ADR-016` § 1 existe pour permettre.

    Un portable sans carte pilote une capacite louee pour ce qui est lourd et
    nettoie sur place ce qui est presque gratuit. Le mode appartient donc a
    chaque entree, pas a l'agent qui les porte.
    """
    engines = agent.parse_engines(
        "chatterbox|voice.chatterbox|1|vast+serverless://Timbre/convert,"
        "deepfilternet|audio.deepfilternet|1|http://deepfilternet:18100|LOCAL_CPU"
    )

    assert [engine.mode for engine in engines] == ["PROXY", "LOCAL_CPU"]


def test_un_moteur_local_sans_precision_reste_ce_que_la_ferme_sert():
    """Sans cinquieme champ, la declaration ne change pas de sens.

    Toute la ferme d'aujourd'hui est locale et sur carte. Un agent mis a jour
    sans que son `ABO_ENGINES` le soit doit continuer a dire vrai.
    """
    engine, = agent.parse_engines("qwen3_tts|voice.qwen3-tts|2|http://qwen3-tts:18100")

    assert engine.mode == "LOCAL_GPU"


def test_le_mode_declare_ne_peut_pas_contredire_l_adresse():
    """Se declarer local sur une adresse louee ferait mentir le placement.

    Un differe partirait sur une capacite facturee a l'appel, ce que `specs/16`
    interdit — et l'erreur ne se verrait que sur la facture.
    """
    with pytest.raises(SystemExit, match="est un PROXY"):
        agent.Engine(
            "clearervoice", "audio.clearervoice", 1,
            "vast+serverless://Clearer%20Voice/enhance", "LOCAL_GPU",
        )

    with pytest.raises(SystemExit, match="pilote une capacite externe"):
        agent.Engine(
            "deepfilternet", "audio.deepfilternet", 1,
            "http://deepfilternet:18100", "PROXY",
        )

    with pytest.raises(SystemExit, match="Mode d'execution inconnu"):
        agent.Engine(
            "deepfilternet", "audio.deepfilternet", 1,
            "http://deepfilternet:18100", "LOCAL_TPU",
        )


def test_le_relais_route_avec_la_cle_endpoint_et_transmet_lenveloppe_vast(monkeypatch):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "console.test":
            assert request.headers["Authorization"] == "Bearer account-secret"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 35490,
                            "endpoint_name": "Clearer Voice",
                            "api_key": "endpoint-secret",
                        }
                    ]
                },
            )
        if request.url.host == "router.test":
            assert request.headers["Authorization"] == "Bearer endpoint-secret"
            body = json.loads(request.content)
            assert body["endpoint"] == "Clearer Voice"
            assert body["request_idx"] == 0
            return httpx.Response(
                200,
                json={
                    "request_idx": 17,
                    "url": "http://worker.test:8000",
                    "signature": "signed",
                },
            )
        assert request.url == httpx.URL(
            "http://worker.test:8000/enhance?api_key=endpoint-secret"
        )
        assert request.headers["Authorization"] == "Bearer endpoint-secret"
        body = json.loads(request.content)
        assert body["auth_data"]["signature"] == "signed"
        assert body["session_id"] is None
        assert body["payload"] == {"audio_b64": "YXVkaW8=", "config": {}}
        return httpx.Response(200, json={"audio_b64": "cmVzdWx0", "format": "wav"})

    monkeypatch.setattr(agent, "VAST_CONSOLE_URL", "https://console.test")
    monkeypatch.setattr(agent, "VAST_SERVERLESS_URL", "https://router.test")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        relay = agent.VastServerlessRelay(client, api_key="account-secret")
        relay.validate([_engine()])
        response = relay.post(_engine(), {"audio_b64": "YXVkaW8=", "config": {}})

    assert response.json()["audio_b64"] == "cmVzdWx0"
    assert len(requests) == 3


def test_une_vraie_session_route_une_fois_puis_reste_collee(monkeypatch):
    """L'affinite est ce que la sortie differee repose dessus, en entier.

    Ce cas fait tourner le **vrai** relais et la **vraie** session, pas un
    double : un double avait deja masque un defaut — le `close()` du relais
    s'etait retrouve dans `VastSession`, qui n'a pas de client TLS, et
    `session.close()` aurait leve un `AttributeError` en production. C'est le
    linter qui l'a vu, pas les tests, et ce cas existe pour que ca ne se
    reproduise pas.

    Ce qu'il affirme : **une seule route pour toute la session**, `session_id`
    sur chaque appel, et l'adresse qui ne change jamais.
    """
    routes = []
    appels = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "console.test":
            return httpx.Response(
                200,
                json={"results": [{"endpoint_name": "Clearer Voice", "api_key": "s"}]},
            )
        if request.url.host == "router.test":
            routes.append(1)
            return httpx.Response(
                200, json={"request_idx": 7, "url": "http://worker.test:8000"}
            )
        corps = json.loads(request.content)
        appels.append((str(request.url), corps.get("session_id")))
        if request.url.path == "/session/create":
            return httpx.Response(200, json={"session_id": "sess-9"})
        return httpx.Response(200, json=RENDU_DIFFERE)

    monkeypatch.setattr(agent, "VAST_CONSOLE_URL", "https://console.test")
    monkeypatch.setattr(agent, "VAST_SERVERLESS_URL", "https://router.test")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        relay = agent.VastServerlessRelay(client, api_key="account-secret")
        monkeypatch.setattr(relay, "_worker_client", lambda _url: client)
        session = relay.open_session(_engine(), lifetime=42.0)
        session.post("/enhance", {"audio_b64": "YQ=="})
        session.post("/upload", {"output_id": "f" * 32})
        session.close()

    assert len(routes) == 1, "la session a redemande une route"
    assert [chemin.split("worker.test:8000")[1].split("?")[0] for chemin, _ in appels] == [
        "/session/create",
        "/enhance",
        "/upload",
        "/session/end",
    ]
    # `session/create` n'a pas encore d'identifiant ; tout le reste le porte.
    assert [identifiant for _, identifiant in appels] == [
        None,
        "sess-9",
        "sess-9",
        "sess-9",
    ]


def test_une_session_fermee_par_le_worker_ne_se_reroute_pas(monkeypatch):
    """`410` veut dire que l'instance a disparu, et ca ne se rattrape pas ici.

    Rerouter ailleurs perdrait le fichier temporaire en croyant reessayer. Le
    job repart par la reprise, qui sait deja le faire (`specs/17`).
    """
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "console.test":
            return httpx.Response(
                200, json={"results": [{"endpoint_name": "Clearer Voice", "api_key": "s"}]}
            )
        if request.url.host == "router.test":
            return httpx.Response(200, json={"request_idx": 1, "url": "http://w.test"})
        if request.url.path == "/session/create":
            return httpx.Response(200, json={"session_id": "sess-1"})
        return httpx.Response(410, json={"error": "invalid session"})

    monkeypatch.setattr(agent, "VAST_CONSOLE_URL", "https://console.test")
    monkeypatch.setattr(agent, "VAST_SERVERLESS_URL", "https://router.test")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        relay = agent.VastServerlessRelay(client, api_key="account-secret")
        monkeypatch.setattr(relay, "_worker_client", lambda _url: client)
        session = relay.open_session(_engine(), lifetime=10.0)
        with pytest.raises(agent.EngineError, match="fermee par le worker"):
            session.post("/enhance", {})


def test_le_numero_de_requete_est_repris_pendant_le_demarrage_a_froid(monkeypatch):
    route_calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "console.test":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "endpoint_name": "Clearer Voice",
                            "api_key": "endpoint-secret",
                        }
                    ]
                },
            )
        if request.url.host == "router.test":
            body = json.loads(request.content)
            route_calls.append(body["request_idx"])
            if len(route_calls) == 1:
                return httpx.Response(200, json={"reqnum": 41})
            return httpx.Response(
                200,
                json={"request_idx": 41, "url": "http://worker.test"},
            )
        return httpx.Response(200, json={"audio_b64": "cmVzdWx0"})

    monkeypatch.setattr(agent, "VAST_CONSOLE_URL", "https://console.test")
    monkeypatch.setattr(agent, "VAST_SERVERLESS_URL", "https://router.test")
    monkeypatch.setattr(agent, "VAST_ROUTE_POLL_SECONDS", 0)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        relay = agent.VastServerlessRelay(client, api_key="account-secret")
        relay.post(_engine(), {"audio_b64": "YXVkaW8="})

    assert route_calls == [0, 41]


class _BackendWithoutStorage:
    """Un backend qui ne sait delivrer aucune concession.

    C'est le cas du repli : l'agent doit alors rendre les octets sous la forme
    que ce backend comprend, plutot que de perdre le travail.
    """

    def deposit_grant(self, *args, **kwargs):
        return None

    def deposit_grant_for(self, *args, **kwargs):
        return None


class _BackendAvecConcession(_BackendWithoutStorage):
    def __init__(self, concession=None):
        self.demandes = []
        self._concession = CONCESSION if concession is None else concession

    def deposit_grant_for(self, job_id, attempt, kind, size_bytes, sha256, content_type):
        self.demandes.append((job_id, attempt, kind, size_bytes, sha256, content_type))
        return self._concession


class _SessionFactice:
    def __init__(self, appels, rendu):
        self.appels = appels
        self._rendu = rendu
        self.fermee = False

    def post(self, route, payload):
        self.appels.append((route, payload))
        if route == "/upload":
            return _reponse({"deposited": True, "status": 200})
        return _reponse(self._rendu)

    def close(self):
        self.fermee = True


class _RelayResult:
    """Le relais Vast, avec et sans session.

    `rendu` est ce que la route du moteur renvoie : la forme differee par
    defaut, puisque c'est ce qu'un moteur a jour rend. `sessions=False` simule
    un endpoint qui les refuse.
    """

    def __init__(self, rendu=None, sessions=True):
        self.calls = []
        self.appels_session = []
        self.sessions = sessions
        self.session = None
        self._rendu = RENDU_DIFFERE if rendu is None else rendu

    def open_session(self, engine, lifetime):
        if not self.sessions:
            raise agent.EngineError("Session Vast refusee (503)")
        self.lifetime = lifetime
        self.session = _SessionFactice(self.appels_session, self._rendu)
        return self.session

    def post(self, engine, payload):
        self.calls.append((engine, payload))
        return _reponse(
            {
                "audio_b64": base64.b64encode(SORTIE).decode(),
                "format": "wav",
                "size_bytes": len(SORTIE),
                "engine": "vast-test",
            }
        )


def test_la_forme_ancienne_par_octets_atteint_encore_vast():
    """Un bail qui porte deja les octets n'a pas de reference a confier.

    Ce test s'appelait « le bail est hydrate avant que Vast voie le job », ce
    qui enonçait la regle inverse de celle d'`ADR-016` § 4 : un `PROXY` ne
    relaie **pas** les octets. Il ne mesurait pourtant que la forme ancienne
    d'`ADR-010` § 6, ou l'entree arrive en `audioB64` et ou il n'y a rien
    d'autre a faire. Le nom promettait une regle que le cas n'etablissait pas —
    exactement ce qui fait qu'un lecteur pressé apprend le contraire du vrai.
    """
    relay = _RelayResult()
    backend = _BackendAvecConcession()

    with httpx.Client() as client:
        result = agent.execute(
            client,
            {"clearervoice": _engine()},
            _assignation({"audioB64": base64.b64encode(b"source").decode()}),
            backend,
            relay,
        )

    # L'entree part en octets — il n'y a pas de reference a confier — mais elle
    # part **dans la session**, parce que la sortie, elle, ne revient pas.
    route, envoye = relay.appels_session[0]
    assert route == "/enhance"
    assert envoye == {
        "audio_b64": base64.b64encode(b"source").decode(),
        "config": {"mode": "denoise"},
        "defer_output": True,
    }
    assert result["outputMediaId"] == "media-1"
    assert result["metrics"]["enginePath"] == "vast-test"


class _BackendQuiCompteSesLectures(_BackendAvecConcession):
    """Un backend qui note chaque octet qu'on lui fait relayer.

    C'est la mesure du critere d'`ABOB-133` : le `PROXY` ne doit transporter
    **aucun octet d'utilisateur**. Ici on ne peut pas regarder une interface
    reseau, mais on peut constater que l'agent n'a jamais suivi la concession.
    """

    def __init__(self):
        super().__init__()
        self.fetches = []

    def fetch(self, grant):
        self.fetches.append(grant)
        return b"source"


SHA_SOURCE = "41cf6794ba4200b839c53531555f0f3998df4cbb01a4d5cb0b94e3ca5e23947d"

_CONCESSION = {
    "grant": {"url": "https://r2.test/travail/objet?sig=abc", "method": "GET"},
    "sha256": SHA_SOURCE,
}


def _assignation(entree: dict) -> dict:
    return {
        "jobId": "job-1",
        "attempt": 1,
        "operation": "AUDIO_ENHANCE",
        "engineKey": "clearervoice",
        "engineConfig": {"mode": "denoise"},
        "input": entree,
    }


def test_un_moteur_loue_recoit_la_reference_et_pas_les_octets():
    """Le coeur d'`ADR-016` § 4 : un `PROXY` pilote, il ne relaie pas.

    La machine louee lit sur R2 elle-meme. L'agent ne suit donc **jamais** la
    concession — c'est ce que compte `fetches`, et c'est la seule assertion qui
    prouve que la ligne du porteur reste libre.
    """
    relay = _RelayResult()
    backend = _BackendQuiCompteSesLectures()

    with httpx.Client() as client:
        resultat = agent.execute(
            client, {"clearervoice": _engine()}, _assignation({"audio": _CONCESSION}), backend, relay
        )

    route, envoye = relay.appels_session[0]
    assert route == "/enhance"
    assert envoye == {
        "audio_url": "https://r2.test/travail/objet?sig=abc",
        # L'empreinte part avec la reference : l'agent ne lit plus, donc il ne
        # peut plus verifier, donc le moteur reprend ce controle.
        "audio_sha256": SHA_SOURCE,
        "config": {"mode": "denoise"},
        "defer_output": True,
    }

    # **Les deux sens, et c'est le critere d'`ABOB-133` en entier.** L'agent ne
    # suit aucune concession de lecture, et la sortie ne remonte pas : ce qui
    # traverse sa ligne est du JSON.
    assert backend.fetches == [], "le PROXY a relaye des octets en entree"
    assert relay.calls == [], "un appel hors session a eu lieu"
    assert "audioB64" not in resultat, "la sortie a traverse le porteur"
    assert resultat["outputMediaId"] == "media-1"
    assert resultat["outputSha256"] == SHA_SORTIE


def test_la_concession_de_depot_est_signee_sur_ce_que_le_moteur_a_annonce():
    """Taille et empreinte **exactes**, donc `ADR-011` tient tel quel.

    C'est ce qui rend le modele d'Onin meilleur que la concession non bornee que
    j'avais proposee : les octets existent au moment de signer, ils sont juste
    ailleurs. Le backend signe donc comme d'habitude, et R2 oppose.
    """
    relay = _RelayResult()
    backend = _BackendAvecConcession()

    with httpx.Client() as client:
        agent.execute(
            client, {"clearervoice": _engine()}, _assignation({"audio": _CONCESSION}), backend, relay
        )

    assert backend.demandes == [
        ("job-1", 1, "output", len(SORTIE), SHA_SORTIE, "audio/wav")
    ]

    route, depot = relay.appels_session[1]
    assert route == "/upload"
    assert depot == {
        "output_id": "f" * 32,
        "put_url": CONCESSION["url"],
        # Tels quels : ils portent la taille et l'empreinte signees.
        "headers": CONCESSION["headers"],
    }
    assert relay.session.fermee, "la session est restee ouverte"
    assert relay.lifetime == agent.VAST_SESSION_LIFETIME


@pytest.mark.parametrize(
    "concession",
    [
        # Une adresse relative designe l'API d'ABO et exige le secret de cette
        # machine : la donner a une capacite louee reviendrait a l'offrir.
        {"mediaId": "m", "url": "/v1/workers/wk/jobs/job-1/media", "headers": {}},
        # Pas de concession du tout : backend anterieur a la decision.
        None,
    ],
)
def test_sans_concession_confiable_la_sortie_revient_en_octets(concession):
    """Le repli existe et il est complet : on ne perd jamais le travail.

    Un chemin qui echouerait en silence serait pire que son absence — la
    machine aurait calcule, et le resultat serait perdu pour une raison
    d'infrastructure que l'utilisateur ne peut pas comprendre.
    """
    relay = _RelayResult()
    backend = _BackendAvecConcession(concession) if concession else _BackendWithoutStorage()

    with httpx.Client() as client:
        resultat = agent.execute(
            client,
            {"clearervoice": _engine()},
            _assignation({"audioB64": base64.b64encode(b"source").decode()}),
            backend,
            relay,
        )

    # Le premier appel a bien tente la voie differee, puis l'agent a rejoue en
    # base64 hors session.
    assert relay.appels_session[0][0] == "/enhance"
    assert len(relay.appels_session) == 1, "un depot a eu lieu sans concession confiable"
    assert relay.calls, "le repli n'a pas rejoue le travail"
    assert resultat["audioB64"] == base64.b64encode(SORTIE).decode()


@pytest.mark.parametrize(
    "grant",
    [
        # Une adresse relative designe l'API d'ABO, et l'agent y ajoute le
        # secret de la machine : la passer a un moteur loue le lui donnerait.
        {"url": "/v1/workers/wk/media/xyz", "method": "GET"},
        # Une concession a en-tetes ne se suit pas sans eux, et un moteur n'en
        # envoie aucun : un `x-amz-*` non signe ferait refuser toute la requete.
        {"url": "https://r2.test/o", "method": "GET", "headers": {"x-amz-meta": "1"}},
        # Un verbe d'ecriture n'est pas une lecture.
        {"url": "https://r2.test/o", "method": "PUT"},
    ],
)
def test_une_concession_qu_on_ne_peut_pas_confier_retombe_sur_les_octets(grant):
    """L'optimisation qui echoue doit hydrater, jamais fabriquer un `403`.

    Chacun de ces trois cas est une raison de **ne pas** confier l'adresse. Le
    base64 marche toujours : preferer un chemin muet serait pire que l'absence
    d'optimisation.
    """
    relay = _RelayResult()
    backend = _BackendQuiCompteSesLectures()
    entree = {"audio": {"grant": grant, "sha256": SHA_SOURCE}}

    with httpx.Client() as client:
        agent.execute(client, {"clearervoice": _engine()}, _assignation(entree), backend, relay)

    # L'entree est hydratee, mais la **sortie** ne revient pas pour autant :
    # les deux sens sont independants, et seul celui-ci retombe.
    route, envoye = relay.appels_session[0]
    assert route == "/enhance"
    assert "audio_url" not in envoye
    assert envoye["audio_b64"] == base64.b64encode(b"source").decode()
    assert backend.fetches == [grant]
    assert relay.appels_session[1][0] == "/upload"


def test_un_moteur_local_recoit_toujours_les_octets():
    """La forme locale ne change pas, et c'est voulu.

    Sur un reseau Docker le base64 ne se paie pas, et le moteur reste **sans
    acces sortant** — lui donner R2 elargirait sa surface sans rien acheter.
    """
    local = agent.Engine("clearervoice", "audio.clearervoice", 1, "http://clearervoice:18100")
    backend = _BackendQuiCompteSesLectures()

    champs = agent.audio_fields({"audio": _CONCESSION}, "audio", backend, local)

    assert champs == {"audio_b64": base64.b64encode(b"source").decode()}
    assert backend.fetches == [_CONCESSION["grant"]]


def test_une_operation_avec_etat_natteint_jamais_vast():
    relay = _RelayResult()
    assignment = {
        "jobId": "job-1",
        "attempt": 1,
        "operation": "TTS",
        "engineKey": "clearervoice",
        "input": {"text": "secret"},
    }

    with (
        httpx.Client() as client,
        pytest.raises(agent.EngineError, match="avec etat interdite"),
    ):
        agent.execute(
            client,
            {"clearervoice": _engine()},
            assignment,
            _BackendWithoutStorage(),
            relay,
        )

    assert relay.calls == []


def test_une_route_dun_autre_moteur_natteint_jamais_vast():
    relay = _RelayResult()
    assignment = {
        "jobId": "job-1",
        "attempt": 1,
        "operation": "AUDIO_ENHANCE",
        "engineKey": "clearervoice",
        "input": {"audioB64": base64.b64encode(b"source").decode()},
    }

    with (
        httpx.Client() as client,
        pytest.raises(agent.EngineError, match="Route Vast incoherente"),
    ):
        agent.execute(
            client,
            {"clearervoice": _engine("convert")},
            assignment,
            _BackendWithoutStorage(),
            relay,
        )

    assert relay.calls == []


def test_un_reenrolement_ne_sonde_pas_une_url_serverless(monkeypatch):
    waits = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class FakeBackend:
        def __init__(self, client):
            pass

        def enrol(self, engines):
            pass

        def heartbeat(self, running):
            return {"mustEnrol": True}

        def lease(self):
            raise SystemExit("fin du test")

    class FakeRelay:
        def __init__(self, client):
            pass

        def validate(self, engines):
            pass

        def close(self):
            pass

    monkeypatch.setattr(agent, "WORKER_KEY", "wk_test")
    monkeypatch.setattr(agent, "WORKER_SECRET", "secret")
    monkeypatch.setattr(
        agent,
        "ENGINES_SPEC",
        "clearervoice|audio.clearervoice|1|vast+serverless://Clearer%20Voice/enhance",
    )
    monkeypatch.setattr(agent.httpx, "Client", FakeClient)
    monkeypatch.setattr(agent, "Backend", FakeBackend)
    monkeypatch.setattr(agent, "VastServerlessRelay", FakeRelay)
    monkeypatch.setattr(
        agent,
        "wait_for_engines",
        lambda client, engines: waits.append([engine.url for engine in engines]),
    )

    with pytest.raises(SystemExit, match="fin du test"):
        agent.run()

    assert waits == [[], []]
