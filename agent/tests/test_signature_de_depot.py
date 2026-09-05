"""L'agent signe ses propres depots (`ADR-017`).

**Ce signeur est un doublon de celui du backend**, et c'est le risque de ce
fichier : deux implementations de SigV4 dans deux depots sans paquet commun.
Ce qui le borne n'est pas ces tests — c'est que R2 refuse tout ce qui s'ecarte
de la norme, donc qu'une divergence rend `403` et jamais un silence.

**Eprouve contre le vrai R2 le 05/09**, et c'est la mesure qui compte : une URL
signee par ce code rend `200`, le meme corps altere rend `400 BadDigest`, et une
cle hors prefixe est refusee ici avant d'atteindre R2. Ces cas-ci tiennent la
forme ; la mesure tient l'accord.

    python -m pytest tests -q
"""
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

import agent

CORPS = b"une sortie de moteur" * 32
EMPREINTE = hashlib.sha256(CORPS).hexdigest()
PREFIXE = "workers/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/"


def _coffre(reste: timedelta = timedelta(hours=6)) -> agent.Vault:
    return agent.Vault(
        access_key_id="AK-temporaire",
        secret_access_key="SK-temporaire",
        session_token="JETON-DE-SESSION",
        prefix=PREFIXE,
        expires_at=datetime.now(UTC) + reste,
        bucket="abo-work",
        endpoint_url="https://" + "a" * 32 + ".r2.cloudflarestorage.com",
        region="auto",
    )


def test_la_taille_et_l_empreinte_sont_signees_donc_opposables():
    """C'est ce qui permet de confier cette URL a une capacite louee.

    Elle n'autorise pas « ecrire cet objet », elle autorise « ecrire **ces
    octets-la** dans cet objet ». Mesure du 05/09 : un corps de la meme taille
    mais different rend `400 BadDigest`.
    """
    url, joints = _coffre().presign_put(PREFIXE + "job/1/output.wav", len(CORPS), EMPREINTE)
    query = parse_qs(urlsplit(url).query)

    assert joints["content-length"] == str(len(CORPS))
    assert joints["x-amz-checksum-sha256"] == base64.b64encode(
        bytes.fromhex(EMPREINTE)
    ).decode()
    # Les deux doivent etre **dans la signature**, sinon le porteur pourrait les
    # retirer et R2 n'opposerait plus rien.
    signes = query["X-Amz-SignedHeaders"][0].split(";")
    assert "content-length" in signes
    assert "x-amz-checksum-sha256" in signes
    assert "host" in signes
    # Le jeton de session voyage dans la query signee, pas en en-tete.
    assert query["X-Amz-Security-Token"] == ["JETON-DE-SESSION"]


def test_une_cle_hors_du_prefixe_est_refusee_ici_et_pas_par_r2():
    """R2 la refuserait aussi, mais il dirait « Access Denied ».

    Personne ne verrait alors que la cause est un prefixe. Refuser ici coute
    trois lignes et nomme le defaut.
    """
    coffre = _coffre()

    with pytest.raises(agent.EngineError, match="hors du prefixe"):
        coffre.presign_put("workers/quelqu-un-d-autre/x.wav", 10, EMPREINTE)
    with pytest.raises(agent.EngineError, match="hors du prefixe"):
        coffre.presign_put("sortie-a-la-racine.wav", 10, EMPREINTE)
    # Le piege du prefixe sans barre : `workers/aaa…e` ne doit pas couvrir
    # `workers/aaa…eSUITE`.
    with pytest.raises(agent.EngineError, match="hors du prefixe"):
        coffre.presign_put(PREFIXE.rstrip("/") + "SUITE/x.wav", 10, EMPREINTE)


def test_l_echeance_d_une_concession_ne_depasse_jamais_celle_du_jeu():
    """Une URL signee plus longtemps que ses credentials promet une duree que
    personne ne tient.

    R2 la refuserait passe l'expiration du jeton ; annoncer quinze minutes
    quand il en reste deux ferait chercher la panne ailleurs.
    """
    court = _coffre(reste=timedelta(minutes=3))
    url, _ = court.presign_put(PREFIXE + "x.wav", 10, EMPREINTE, ttl=900)
    assert int(parse_qs(urlsplit(url).query)["X-Amz-Expires"][0]) <= 180

    large = _coffre(reste=timedelta(hours=6))
    url, _ = large.presign_put(PREFIXE + "x.wav", 10, EMPREINTE, ttl=900)
    assert int(parse_qs(urlsplit(url).query)["X-Amz-Expires"][0]) == 900


def test_un_jeu_proche_de_son_echeance_n_est_plus_utilisable():
    """La marge existe pour ne pas signer avec ce qui va mourir.

    Un jeu qui expire pendant le televersement rendrait `403` **au milieu** d'un
    depot — un chapitre calcule, et perdu pour une horloge.
    """
    assert _coffre(reste=timedelta(hours=6)).usable is True
    assert _coffre(reste=timedelta(minutes=5)).usable is False
    assert _coffre(reste=timedelta(minutes=-1)).usable is False


def test_deux_signatures_du_meme_objet_different_par_leur_contenu():
    """Preuve que l'empreinte entre vraiment dans la signature.

    Sans elle, les deux URL seraient identiques et le porteur pourrait deposer
    n'importe quoi a cette adresse.
    """
    coffre = _coffre()
    cle = PREFIXE + "job/1/output.wav"
    une, _ = coffre.presign_put(cle, len(CORPS), EMPREINTE)
    autre, _ = coffre.presign_put(cle, len(CORPS), hashlib.sha256(b"autre").hexdigest())

    def signature(url):
        return parse_qs(urlsplit(url).query)["X-Amz-Signature"][0]

    assert signature(une) != signature(autre)
