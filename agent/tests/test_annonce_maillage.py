"""L'URL de validation remonte a la console, et son echec ne coûte rien.

Ce module est le seul appel que la machine passe **hors** du maillage
(`ABOB-157`). Deux choses valent d'etre eprouvees, et ce sont les deux qui
feraient perdre une machine si elles etaient fausses.

**Il va bien a la surface publique.** L'adresse de maillage n'existe pas encore
a ce moment-la : viser `ABO_BACKEND_URL` reviendrait a appeler une adresse que
la machine ne sait pas joindre, et l'URL de validation ne remonterait jamais.

**Un echec ne l'arrete pas.** L'entrypoint tourne sous `set -e`. Une remontee
qui leve, un backend injoignable, un `4xx` : rien de tout ca ne doit empecher
une machine d'entrer dans le maillage, parce que l'URL est aussi dans le
journal — le chemin qui marchait avant cette fonctionnalite.

    python -m pytest tests/test_annonce_maillage.py -q
"""
import json
import urllib.error
import urllib.request

import pytest

import mesh_announce

URL = "https://login.tailscale.com/a/f4594d20134bb"


@pytest.fixture
def environnement(monkeypatch):
    monkeypatch.setenv("ABO_BACKEND_PUBLIC_URL", "https://api.abo-studio.eu")
    monkeypatch.setenv("ABO_BACKEND_URL", "http://100.124.46.23:8080")
    monkeypatch.setenv("ABO_WORKER_KEY", "wk_bb200444ff62")
    monkeypatch.setenv("ABO_WORKER_SECRET", "un-secret")


@pytest.fixture
def poste(monkeypatch):
    """Capture la requete au lieu de la poster."""
    vues = []

    class _Reponse:
        def read(self):
            return json.dumps({"status": "WAITING"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def _urlopen(requete, timeout=None):
        vues.append(requete)
        return _Reponse()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return vues


def test_l_annonce_va_au_tunnel_public_et_pas_a_l_adresse_de_maillage(environnement, poste):
    """**Le maillage n'existe pas encore pour cette machine.**

    C'est tout le sujet : viser l'adresse de maillage ici appellerait une
    adresse que la machine ne sait pas joindre, et l'URL de validation ne
    remonterait jamais. La console resterait vide, et il faudrait retourner
    lire un journal — donc la fonctionnalite entiere ne servirait a rien.
    """
    assert mesh_announce.announce(URL, "abo-wk-bb200444ff62") is True

    (requete,) = poste
    assert requete.full_url == (
        "https://api.abo-studio.eu/v1/mesh/wk_bb200444ff62/enrolment"
    )
    assert "100.124.46.23" not in requete.full_url
    corps = json.loads(requete.data)
    assert corps == {"loginUrl": URL, "nodeName": "abo-wk-bb200444ff62"}


def test_le_client_se_nomme_car_urllib_anonyme_est_bloque(environnement, poste):
    """Mesure du 05/09 contre le vrai tunnel, et elle a coûté une épreuve.

    Cloudflare refuse le `User-Agent` par défaut d'`urllib` —
    `Python-urllib/3.12` — avec un `403` portant son propre code `1010`. Il
    ressemble à un refus d'ABO et n'en est pas un : la requête n'atteint jamais
    l'application. Tout autre nom rend `401`, c'est-à-dire ABO qui répond.
    """
    mesh_announce.announce(URL, "abo-wk-x")

    (requete,) = poste
    envoye = requete.get_header("User-agent")
    assert envoye, "aucun User-Agent : urllib poserait le sien, et il est bloqué"
    assert "urllib" not in envoye.lower()
    assert envoye == mesh_announce.AGENT


def test_le_secret_voyage_en_entete_et_jamais_en_argument(environnement, poste):
    """`ps` est lisible par tout ce qui tourne dans le conteneur.

    Un secret passe en argument de commande y resterait visible le temps de
    l'appel — et l'appel attend un reseau, donc ce temps n'est pas negligeable.
    """
    mesh_announce.announce(URL, "abo-wk-x")

    (requete,) = poste
    # `urllib` capitalise les noms d'en-tete qu'on lui donne.
    assert requete.get_header("X-worker-secret") == "un-secret"
    assert "un-secret" not in requete.full_url


@pytest.mark.parametrize(
    "panne",
    [
        urllib.error.HTTPError("u", 401, "unauthorized", {}, None),
        urllib.error.URLError("nom introuvable"),
        TimeoutError("depasse"),
        RuntimeError("quelque chose d'inattendu"),
    ],
)
def test_aucune_panne_de_remontee_n_arrete_l_enrolement(environnement, monkeypatch, panne):
    """L'entrypoint tourne sous `set -e`.

    Une exception qui remonte tuerait le conteneur **apres** que Tailscale a
    ecrit son URL, donc juste avant le moment ou un humain allait cliquer. On
    perdrait la machine pour un confort. C'est pourquoi le `except` est large
    ici et etroit partout ailleurs.
    """

    def _tombe(*_args, **_kwargs):
        raise panne

    monkeypatch.setattr(urllib.request, "urlopen", _tombe)

    assert mesh_announce.announce(URL, "abo-wk-x") is False
    assert mesh_announce.settle("100.124.46.23", "abo@example.com") is False
    assert mesh_announce.fail("un motif") is False
    # Et par le chemin que l'entrypoint emprunte vraiment : un code de retour
    # non nul ferait tomber `set -e` malgre le `|| true`... qui le rattrape,
    # mais rien ne garantit que le prochain appelant l'ecrira.
    assert mesh_announce.main(["announce", URL, "abo-wk-x"]) == 0
    assert mesh_announce.main(["settled", "100.124.46.23"]) == 0
    assert mesh_announce.main(["failed", "un motif"]) == 0


def test_sans_adresse_publique_configuree_rien_n_est_tente(monkeypatch, poste):
    """C'est le mode d'avant, et il reste valable.

    Sans cette variable, l'URL vit dans le journal comme elle l'a toujours
    fait. Refuser de demarrer serait imposer une console a qui n'en veut pas.
    """
    monkeypatch.delenv("ABO_BACKEND_PUBLIC_URL", raising=False)
    monkeypatch.setenv("ABO_WORKER_KEY", "wk_x")
    monkeypatch.setenv("ABO_WORKER_SECRET", "s")

    assert mesh_announce.announce(URL, "abo-wk-x") is False
    assert poste == []


def test_l_issue_distingue_l_adresse_obtenue_du_motif_d_echec(environnement, poste):
    """Deux issues, une seule route : c'est la presence de l'adresse qui tranche.

    Le backend en depend — sans adresse il fait echouer la demande avec le
    motif. Envoyer une adresse vide **et** un motif serait ambigu, et
    l'ambiguite se resoudrait du mauvais cote un jour.
    """
    mesh_announce.settle("100.124.46.23", "abo@example.com")
    mesh_announce.fail("aucune validation en 900s")

    reussi, echoue = (json.loads(requete.data) for requete in poste)
    assert reussi["meshAddress"] == "100.124.46.23"
    assert reussi["validatedBy"] == "abo@example.com"
    assert echoue["detail"] == "aucune validation en 900s"
    # Absente, et pas vide : le backend traite les deux pareil — son champ
    # vaut `""` par defaut — mais ne rien envoyer dit ce qui s'est passe.
    assert "meshAddress" not in echoue
    assert all(
        requete.full_url.endswith("/enrolment/settled") for requete in poste
    )
