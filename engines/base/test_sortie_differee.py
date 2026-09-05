"""La sortie differee : garder, deposer, oublier (`ADR-016` § 4).

Ce qui est eprouve ici n'est pas « ca marche » mais les quatre refus qui
empechent un depot d'aller ailleurs qu'a l'endroit prevu, plus le fait qu'un
depot reussi ne laisse rien derriere.

    python -m pytest engines/base -q
"""
import hashlib

import aboengine
import pytest

OCTETS = b"RIFF" + b"une sortie de moteur" * 64


@pytest.fixture(autouse=True)
def _repertoire_isole(tmp_path, monkeypatch):
    """Chaque cas a son propre repertoire de sorties.

    Le module en garde un global : le partager ferait qu'un cas voit les
    temporaires d'un autre, et le montage ferait partie de ce que le test
    affirme sans que personne le sache.
    """
    monkeypatch.setattr(aboengine, "OUTPUT_ROOT", tmp_path / "sorties")


def test_une_sortie_gardee_rend_de_quoi_signer_une_concession():
    """Taille et empreinte **exactes**, calculees sur les octets a deposer.

    C'est ce qui permet au backend de signer la concession comme il le fait
    deja (`ADR-011`) : rien n'est affaibli par le fait que le calcul ait eu lieu
    ailleurs.
    """
    garde = aboengine.keep(OCTETS)

    assert garde["sha256"] == hashlib.sha256(OCTETS).hexdigest()
    assert aboengine._output_path(garde["output_id"]).read_bytes() == OCTETS


def test_la_forme_differee_ne_porte_aucun_octet():
    """Le porteur ne recoit que la designation, jamais l'audio.

    C'est la moitie du critere d'`ABOB-133` qui se verifie sans Vast : si
    `audio_b64` reapparaissait ici, le `PROXY` relaierait a nouveau tout.
    """
    audio = aboengine.to_wav([3000, -3000] * 8000, 16000)

    differe = aboengine.rendered(audio, "essai", defer=True)
    direct = aboengine.rendered(audio, "essai", defer=False)

    assert "audio_b64" not in differe
    assert differe["output_id"] and differe["sha256"]
    assert differe["size_bytes"] == len(audio)
    # La forme locale ne bouge pas, et c'est voulu.
    assert direct["audio_b64"] and "output_id" not in direct


def test_une_sortie_muette_n_obtient_jamais_d_identifiant():
    """L'ordre des controles est ce que ce cas affirme.

    Le silence est refuse **avant** que la sortie soit gardee : le porteur n'a
    donc rien a deposer, plutot que de decouvrir le vide apres avoir fait
    signer une concession — et plutot que de facturer du souffle.
    """
    with pytest.raises(aboengine.AudioError, match="silence"):
        aboengine.rendered(aboengine.to_wav([0] * 16000, 16000), "essai", defer=True)

    assert not aboengine.OUTPUT_ROOT.exists() or not list(aboengine.OUTPUT_ROOT.iterdir())


@pytest.mark.parametrize(
    "identifiant",
    ["../../etc/passwd", "..", "a" * 31, "A" * 32, "", "zz" + "0" * 30],
)
def test_un_identifiant_qui_n_est_pas_des_notres_est_refuse(identifiant):
    """`output_id` revient **du reseau**, et le porteur n'est pas forcement
    celui qui l'a genere.

    Sans cette borne, un `../` ferait deposer ou effacer n'importe quel fichier
    du conteneur.
    """
    with pytest.raises(aboengine.AudioError, match="output_id invalide"):
        aboengine._output_path(identifiant)

    assert aboengine.drop(identifiant) is False


def test_une_concession_qui_n_est_pas_en_https_est_refusee():
    garde = aboengine.keep(OCTETS)
    with pytest.raises(aboengine.AudioError, match="doit etre en https"):
        aboengine.deposit(garde["output_id"], "http://r2.test/objet")


class _Reponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_un_depot_envoie_les_octets_exacts_et_les_entetes_tels_quels(monkeypatch):
    """Les en-tetes de la concession partent **sans etre touches**.

    Ce sont eux qui portent la taille et l'empreinte signees : en ajouter ou en
    retirer un ferait refuser toute la requete par R2, `403`, et l'oubli ne se
    rattraperait nulle part plus loin.

    Et le temporaire disparait, sinon un depot reussi laisserait de quoi le
    rejouer.
    """
    vues = []

    def _urlopen(requete, timeout=None):
        vues.append((requete.method, requete.full_url, dict(requete.headers), requete.data))
        return _Reponse()

    monkeypatch.setattr(aboengine.urllib.request, "urlopen", _urlopen)

    garde = aboengine.keep(OCTETS)
    entetes = {"content-length": str(len(OCTETS)), "x-amz-checksum-sha256": "Zm9v"}
    resultat = aboengine.deposit(garde["output_id"], "https://r2.test/objet", entetes)

    methode, adresse, envoyes, corps = vues[0]
    assert (methode, adresse) == ("PUT", "https://r2.test/objet")
    assert corps == OCTETS
    # urllib met les noms en Capitalise ; c'est la paire qui compte.
    assert {c.lower(): v for c, v in envoyes.items()} == entetes
    assert resultat["deposited"] is True

    assert not aboengine._output_path(garde["output_id"]).exists()
    with pytest.raises(aboengine.AudioError, match="deja deposee"):
        aboengine.deposit(garde["output_id"], "https://r2.test/objet", entetes)


def test_un_refus_du_stockage_garde_le_temporaire(monkeypatch):
    """Un refus peut venir d'une concession mal signee **de notre cote**.

    Garder le fichier laisse le porteur redemander une concession et rejouer le
    depot sans rien recalculer — un chapitre de trois minutes ne se refait pas
    parce qu'une signature etait fausse. La fermeture de session nettoiera.

    Et le code differe d'`AudioError` a dessein : le contrat moteur rend `502`
    ici, ce qui compte une tentative, la ou `422` dirait « inutile de rejouer ».
    """
    def _urlopen(requete, timeout=None):
        raise aboengine.urllib.error.HTTPError(
            requete.full_url, 403, "SignatureDoesNotMatch", {}, None
        )

    monkeypatch.setattr(aboengine.urllib.request, "urlopen", _urlopen)

    garde = aboengine.keep(OCTETS)
    with pytest.raises(aboengine.DepositError, match="403"):
        aboengine.deposit(garde["output_id"], "https://r2.test/objet", {})

    assert aboengine._output_path(garde["output_id"]).read_bytes() == OCTETS
