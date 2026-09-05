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


def test_le_bail_abo_est_hydrate_avant_que_vast_voie_le_job():
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
