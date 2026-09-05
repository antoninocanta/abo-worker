import base64
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
    def deposit_grant(self, *args, **kwargs):
        return None


class _RelayResult:
    def __init__(self):
        self.calls = []

    def post(self, engine, payload):
        self.calls.append((engine, payload))
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://worker.test/enhance"),
            json={
                "audio_b64": base64.b64encode(b"clean").decode(),
                "format": "wav",
                "size_bytes": 5,
                "engine": "vast-test",
            },
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
    assignment = {
        "jobId": "job-1",
        "attempt": 1,
        "operation": "AUDIO_ENHANCE",
        "engineKey": "clearervoice",
        "engineConfig": {"mode": "denoise"},
        "input": {"audioB64": base64.b64encode(b"source").decode()},
    }

    with httpx.Client() as client:
        result = agent.execute(
            client,
            {"clearervoice": _engine()},
            assignment,
            _BackendWithoutStorage(),
            relay,
        )

    assert relay.calls[0][1] == {
        "audio_b64": base64.b64encode(b"source").decode(),
        "config": {"mode": "denoise"},
    }
    assert result["audioB64"] == base64.b64encode(b"clean").decode()
    assert result["metrics"]["enginePath"] == "vast-test"


class _BackendQuiCompteSesLectures(_BackendWithoutStorage):
    """Un backend qui note chaque octet qu'on lui fait relayer.

    C'est la mesure du critere d'`ABOB-133` : le `PROXY` ne doit transporter
    **aucun octet d'utilisateur**. Ici on ne peut pas regarder une interface
    reseau, mais on peut constater que l'agent n'a jamais suivi la concession.
    """

    def __init__(self):
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
        agent.execute(
            client, {"clearervoice": _engine()}, _assignation({"audio": _CONCESSION}), backend, relay
        )

    assert relay.calls[0][1] == {
        "audio_url": "https://r2.test/travail/objet?sig=abc",
        # L'empreinte part avec la reference : l'agent ne lit plus, donc il ne
        # peut plus verifier, donc le moteur reprend ce controle.
        "audio_sha256": SHA_SOURCE,
        "config": {"mode": "denoise"},
    }
    assert backend.fetches == [], "le PROXY a relaye des octets"


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

    envoye = relay.calls[0][1]
    assert "audio_url" not in envoye
    assert envoye["audio_b64"] == base64.b64encode(b"source").decode()
    assert backend.fetches == [grant]


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
