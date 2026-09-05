"""Faire remonter l'URL de validation du maillage a la console ABO (`ABOB-157`).

L'enrolement au maillage se valide **a la main**, une fois par machine. Le
conteneur ecrivait donc son URL `login.tailscale.com/...` sur sa sortie standard
et attendait quinze minutes : il fallait surveiller un journal pour ne pas
manquer la fenetre, et une machine qui demarre la nuit echouait sans que rien ne
le dise.

**C'est le seul appel que cette machine passe hors du maillage**, et il ne peut
pas etre autrement : au moment ou l'URL existe, la machine n'a pas d'adresse de
maillage. Il va donc a la surface **publique** d'ABO, pas a la surface machine.
D'ou deux adresses distinctes dans l'environnement, et c'est deliberement
visible plutot que devine :

    ABO_BACKEND_PUBLIC_URL   le tunnel — pour cet appel-ci, et rien d'autre
    ABO_BACKEND_URL          l'adresse de maillage — pour tout le travail

Il n'accorde rien : aucun bail, aucun credential, aucune concession d'ecriture.
Il depose une ligne dans une console d'exploitation.

**Un echec ici n'arrete pas l'enrolement.** L'URL reste dans le journal, qui
etait le seul chemin jusqu'ici : perdre le confort ne doit pas coûter la
machine. On le dit fort, et on continue.

    python -m pytest tests/test_annonce_maillage.py -q
"""
import json
import os
import sys
import urllib.error
import urllib.request

# Court a dessein. Cet appel est un confort, et le conteneur a une validation
# humaine a attendre derriere : le faire patienter trente secondes sur un
# backend injoignable retarderait l'affichage de l'URL dans le journal, donc le
# seul chemin qui marche toujours.
DELAI = 10.0


def _poste(chemin: str, corps: dict) -> dict | None:
    racine = (os.getenv("ABO_BACKEND_PUBLIC_URL") or "").rstrip("/")
    cle = os.getenv("ABO_WORKER_KEY") or ""
    secret = os.getenv("ABO_WORKER_SECRET") or ""
    if not (racine and cle and secret):
        # Sans adresse publique configuree, la remontee n'a simplement pas lieu.
        # Ce n'est pas une panne : c'est le mode d'avant, ou l'URL vit dans le
        # journal. On le dit une fois, sans crier.
        print(
            "abo-annonce: ABO_BACKEND_PUBLIC_URL absente — l'URL de validation "
            "reste dans ce journal uniquement",
            file=sys.stderr,
        )
        return None

    requete = urllib.request.Request(
        f"{racine}/v1/mesh/{cle}/enrolment{chemin}",
        data=json.dumps(corps).encode("utf-8"),
        # Le secret voyage en en-tete, jamais en argument de commande : `ps`
        # est lisible par tout ce qui tourne dans le conteneur.
        headers={"Content-Type": "application/json", "X-Worker-Secret": secret},
        method="POST",
    )
    try:
        with urllib.request.urlopen(requete, timeout=DELAI) as reponse:
            return json.loads(reponse.read() or b"{}")
    except urllib.error.HTTPError as erreur:
        # Un `4xx` est un defaut de configuration — mauvais secret, cle
        # inconnue, URL refusee — et il se nomme. Un journal qui dit « echec »
        # sans le code enverrait chercher au mauvais endroit.
        print(
            f"abo-annonce: refus du backend ({erreur.code}) sur"
            f" /v1/mesh/.../enrolment{chemin} : {erreur.read()[:200]!r}",
            file=sys.stderr,
        )
    except Exception as erreur:  # noqa: BLE001 — rien ici ne doit arreter l'enrolement.
        print(f"abo-annonce: backend injoignable ({erreur})", file=sys.stderr)
    return None


def announce(login_url: str, node_name: str) -> bool:
    """« Voici mon URL de validation. » Vrai si la console l'a prise."""
    return _poste("", {"loginUrl": login_url, "nodeName": node_name}) is not None


def settle(mesh_address: str, validated_by: str) -> bool:
    """« J'ai une adresse, et voici le compte qui a valide. »"""
    return (
        _poste("/settled", {"meshAddress": mesh_address, "validatedBy": validated_by})
        is not None
    )


def fail(detail: str) -> bool:
    """« Ca n'a pas abouti, et voici pourquoi. »

    Le motif compte plus que le statut : « valide par le mauvais compte » et
    « personne n'a clique en quinze minutes » demandent deux gestes differents.
    """
    return _poste("/settled", {"detail": detail}) is not None


def main(argv: list[str]) -> int:
    """Appele par l'entrypoint, qui est en `sh`.

    Trois formes, et le code de retour n'est **jamais** un echec : l'entrypoint
    tourne sous `set -e`, et une remontee qui ne passe pas ne doit pas empecher
    une machine d'entrer dans le maillage.

        mesh_announce.py announce <url> <nom-de-noeud>
        mesh_announce.py settled  <adresse> [compte]
        mesh_announce.py failed   <motif>
    """
    if len(argv) < 2:
        print("abo-annonce: usage : announce|settled|failed ...", file=sys.stderr)
        return 0
    verbe, arguments = argv[0], argv[1:]
    if verbe == "announce":
        announce(arguments[0], arguments[1] if len(arguments) > 1 else "")
    elif verbe == "settled":
        settle(arguments[0], arguments[1] if len(arguments) > 1 else "")
    elif verbe == "failed":
        fail(arguments[0])
    else:
        print(f"abo-annonce: verbe inconnu « {verbe} »", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
