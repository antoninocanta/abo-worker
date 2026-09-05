"""Ce que `source()` garantit, et qui n'est pas visible a la lecture.

Ces cas ne testent pas « ca marche » : ils testent les trois refus qui
empechent un moteur de calculer sur la mauvaise matiere sans que personne le
voie. Un resultat produit sur les mauvais octets a le bon format, la bonne
duree, et il est faux.

    python -m pytest engines/base -q
"""
import base64
import hashlib

import pytest

import aboengine

OCTETS = b"une prise a nettoyer"
EMPREINTE = hashlib.sha256(OCTETS).hexdigest()


def test_la_forme_par_octets_reste_la_forme_locale():
    assert aboengine.source(b64=base64.b64encode(OCTETS).decode()) == OCTETS


def test_l_empreinte_annoncee_est_verifiee_et_pas_seulement_transportee():
    """C'est le controle que l'agent perd en passant une adresse.

    Sur la forme base64, c'est lui qui confrontait ce que le stockage rendait a
    ce qui etait annonce. En passant une URL il ne lit plus, donc il ne peut
    plus verifier : le moteur reprend ce controle, sinon la garantie
    disparaitrait en silence.
    """
    encode = base64.b64encode(OCTETS).decode()

    assert aboengine.source(b64=encode, sha256=EMPREINTE) == OCTETS

    with pytest.raises(aboengine.AudioError, match="empreinte inattendue"):
        aboengine.source(b64=encode, sha256="b" * 64)


def test_deux_formes_a_la_fois_est_un_refus_et_pas_une_preference():
    """Choisir silencieusement laisserait l'appelant croire l'autre forme servie.

    C'est l'agent qui decide de la forme selon la distance ; en recevoir deux
    veut dire qu'il s'est trompe, et deviner masquerait son defaut.
    """
    with pytest.raises(aboengine.AudioError, match="deux formes"):
        aboengine.source(b64=base64.b64encode(OCTETS).decode(), url="https://r2.test/o")


def test_une_reference_qui_n_est_pas_en_https_est_refusee():
    """Une concession voyage sur un lien qu'on n'administre pas.

    Et l'exigence ferme au passage `file://` et `data:`, qu'un moteur n'a aucune
    raison de savoir suivre.
    """
    with pytest.raises(aboengine.AudioError, match="doit etre en https"):
        aboengine.source(url="http://r2.test/o")

    with pytest.raises(aboengine.AudioError, match="doit etre en https"):
        aboengine.source(url="file:///etc/passwd")


def test_une_entree_absente_se_dit_plutot_que_de_rendre_du_vide():
    with pytest.raises(aboengine.AudioError, match="ni octets ni reference"):
        aboengine.source(what="reference")
