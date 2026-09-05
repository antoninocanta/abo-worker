#!/bin/sh
# Rejoindre le maillage, **puis** lancer l'agent — jamais l'inverse.
#
# `ADR-015` § 1 : le maillage est la condition d'existence d'un worker. Une
# machine qui n'a pas d'adresse de maillage n'est pas un worker degrade, elle
# n'est pas un worker. Cet entrypoint refuse donc de lancer l'agent plutot que
# de le laisser s'enroler par le tunnel public : ce serait exactement la dette
# que le realignement retire, et elle serait invisible.
#
# Mode **noyau**, avec `/dev/net/tun`. L'objectif « installation en une passe »
# tient parce que le device et les capacites se declarent dans le compose.
#
# **Enrolement manuel, une fois par machine** (arbitre par Onin le 05/09). Pas
# de cle reutilisable distribuee dans les conteneurs, pas de noeud ephemere :
# les workers seront peu nombreux, et chacun se valide a la main dans la console
# Tailscale. Le conteneur affiche son URL de login et attend ; l'etat persiste
# ensuite dans un volume, donc un `restart` ou un `up -d` apres reconstruction
# ne redemande rien.
set -eu

MAILLAGE_ETAT=/var/lib/tailscale/tailscaled.state
MAILLAGE_SOCKET=/run/tailscale/tailscaled.sock
SOCKET_MAX=${ABO_MESH_READY_TIMEOUT:-60}
# Genereux : c'est un humain qui ouvre une URL dans un navigateur.
LOGIN_MAX=${ABO_MESH_LOGIN_TIMEOUT:-900}

ts() { tailscale --socket="$MAILLAGE_SOCKET" "$@"; }

echec() {
    echo "abo-entrypoint: $1" >&2
    exit 1
}

# --- Ce que le conteneur doit avoir recu -------------------------------------
#
# On le verifie ici et pas dans l'agent : c'est un defaut de **deploiement**, et
# le diagnostiquer depuis un journal d'agent coute une location. `ABOB-155` a
# paye ce defaut une fois — la machine tournait, notre code n'existait pas, et
# rien ne disait pourquoi.

[ -c /dev/net/tun ] || echec "/dev/net/tun absent. Le compose doit porter :
    devices:  [ \"/dev/net/tun:/dev/net/tun\" ]
    cap_add:  [ NET_ADMIN, NET_RAW ]
  Sans ce device, tailscaled ne peut pas creer d'interface en mode noyau."

# --- Le demon ----------------------------------------------------------------

mkdir -p /run/tailscale /var/lib/tailscale
tailscaled --state="$MAILLAGE_ETAT" --socket="$MAILLAGE_SOCKET" --tun=tailscale0 &
MAILLAGE_PID=$!

# Un `tailscale up` lance trop tot echoue sur la socket, pas sur le reseau : on
# attend que le demon reponde avant de conclure quoi que ce soit.
attendu=0
while [ ! -S "$MAILLAGE_SOCKET" ]; do
    attendu=$((attendu + 1))
    [ "$attendu" -lt "$SOCKET_MAX" ] || echec "tailscaled n'a pas ouvert sa socket en ${SOCKET_MAX}s."
    kill -0 "$MAILLAGE_PID" 2>/dev/null || echec "tailscaled s'est arrete au demarrage."
    sleep 1
done

# --- Le noeud ----------------------------------------------------------------
#
# **Un nom de noeud est un label DNS, et une cle de machine n'en est pas un.**
# Les cles sont de la forme `wk_ab12cd`, et l'underscore est refuse :
# « "abo-wk_epreuve" is not a valid DNS label ». Sans cette traduction, *aucune*
# machine ne pourrait s'enroler — mesure du 05/09, sur la premiere epreuve.
NOM_NOEUD="abo-$(printf '%s' "${ABO_WORKER_KEY:-sans-cle}" | tr '_' '-' | tr -cd 'a-zA-Z0-9-')"

adresse() { ts ip -4 2>/dev/null | head -n 1; }

# Le compte Tailscale qui a valide ce noeud, ou rien s'il n'est pas encore
# lisible. **Il ne l'est qu'une fois le demon `Running`** : au premier
# enrolement il l'est deja, parce que `tailscale up` a attendu la validation ;
# a la reprise depuis l'etat, l'adresse revient du volume avant que la table
# des comptes soit peuplee. Rendre vide dans ce cas est la bonne reponse — c'est
# a l'appelant de decider s'il attend ou s'il refuse.
compte_de_validation() {
    ts status --json 2>/dev/null | python3 -c '
import json, sys
try:
    etat = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if etat.get("BackendState") != "Running":
    sys.exit(0)
moi = (etat.get("Self") or {}).get("UserID")
utilisateur = (etat.get("User") or {}).get(str(moi)) or {}
print(utilisateur.get("LoginName", ""))
' 2>/dev/null || true
}

# Rempli plus bas, et declare ici pour que `set -u` ne morde pas sur le chemin
# ou aucun controle de compte n'est demande.
COMPTE=""

if [ -n "$(adresse)" ]; then
    echo "abo-entrypoint: deja enrole, etat repris du volume"
else
    # `--accept-dns=false` a dessein : reecrire le resolv.conf du conteneur
    # casserait la resolution de ce qui est **hors** du maillage — R2, Vast, les
    # fournisseurs de langue — et le maillage ne couvre que le lien
    # machine <-> backend (`ADR-015` § 1). On adresse donc le backend
    # explicitement.
    #
    # `--hostname` porte la cle de la machine : un noeud anonyme dans une
    # console de maillage ne se revoque pas, faute de savoir lequel c'est.
    echo "abo-entrypoint: premiere connexion au maillage — validation manuelle attendue"
    echo "abo-entrypoint: ============================================================"
    # La sortie va dans un fichier **et** dans ce journal. Le fichier sert a en
    # extraire l'URL ; le journal reste le chemin qui marche toujours, y compris
    # quand la console est injoignable. Un `| tee` ne conviendrait pas : `$!`
    # rendrait le pid de `tee`, et on surveillerait le mauvais processus.
    JOURNAL_LOGIN=$(mktemp)
    ts up --accept-dns=false --hostname="$NOM_NOEUD" --timeout="${LOGIN_MAX}s" \
        > "$JOURNAL_LOGIN" 2>&1 &
    LOGIN_PID=$!
    tail -f "$JOURNAL_LOGIN" &
    ECHO_PID=$!

    # --- Faire remonter l'URL a la console ABO (`ABOB-157`) ------------------
    #
    # C'est le seul appel que cette machine passe **hors** du maillage, et il ne
    # peut pas etre autrement : au moment ou l'URL existe, la machine n'a pas
    # d'adresse. Il va donc a la surface publique, et il n'accorde rien.
    #
    # `grep` sur le fichier plutot qu'un motif dans la sortie : l'URL parait en
    # une seconde ou deux, bien avant la validation humaine.
    URL_LOGIN=""
    attendu=0
    while [ -z "$URL_LOGIN" ] && [ "$attendu" -lt 30 ]; do
        URL_LOGIN=$(grep -om1 'https://login\.tailscale\.com/[A-Za-z0-9/_.-]*' \
            "$JOURNAL_LOGIN" 2>/dev/null || true)
        [ -n "$URL_LOGIN" ] && break
        kill -0 "$LOGIN_PID" 2>/dev/null || break
        attendu=$((attendu + 1))
        sleep 1
    done
    if [ -n "$URL_LOGIN" ]; then
        # `|| true` : une remontee qui echoue ne doit pas coûter la machine.
        # L'URL est dans le journal, qui etait le seul chemin jusqu'ici.
        python3 /srv/mesh_announce.py announce "$URL_LOGIN" "$NOM_NOEUD" || true
    else
        echo "abo-entrypoint: aucune URL de login reperee — voir ce journal" >&2
    fi

    # `tailscale up` ecrit l'URL puis attend. On surveille l'adresse plutot que
    # la sortie du processus : c'est l'obtention d'une adresse qui prouve
    # l'enrolement, pas un code de retour.
    attendu=0
    while [ -z "$(adresse)" ]; do
        attendu=$((attendu + 5))
        if [ "$attendu" -ge "$LOGIN_MAX" ]; then
            python3 /srv/mesh_announce.py failed \
                "aucune validation en ${LOGIN_MAX}s" || true
            kill "$ECHO_PID" 2>/dev/null || true
            echec "aucune validation en ${LOGIN_MAX}s.
  L'URL de login est plus haut dans ce journal. La rejouer :
    docker compose logs agent | grep login.tailscale.com
  Un worker hors maillage n'est pas un worker (ADR-015 § 1)."
        fi
        kill -0 "$LOGIN_PID" 2>/dev/null || break
        sleep 5
    done
    wait "$LOGIN_PID" 2>/dev/null || true
    kill "$ECHO_PID" 2>/dev/null || true
    rm -f "$JOURNAL_LOGIN"
    echo "abo-entrypoint: ============================================================"
fi

ADRESSE=$(adresse)
if [ -z "$ADRESSE" ]; then
    python3 /srv/mesh_announce.py failed "aucune adresse de maillage obtenue" || true
fi
[ -n "$ADRESSE" ] || echec "aucune adresse de maillage obtenue.
  **La raison exacte est dans les lignes que tailscale vient d'ecrire au-dessus**
  — un nom de noeud invalide, une validation refusee ou expiree echouent toutes
  ici. Ne pas deviner.
  Un worker hors maillage n'est pas un worker (ADR-015 § 1)."

# --- Le compte qui a valide --------------------------------------------------
#
# Controle d'**exploitation**, et Onin l'a pose en sachant ce qu'il vaut : une
# adresse email n'authentifie rien par elle-meme, c'est Tailscale qui
# authentifie. Ce que ce controle attrape est une erreur d'operateur — un noeud
# valide depuis le mauvais compte, donc entre dans le mauvais tailnet, donc dans
# un perimetre de confiance qui n'est pas le notre.
#
# **On refuse plutot qu'on avertit**, parce que le maillage *est* le perimetre de
# confiance de la ferme (`ADR-015` § 1) : un avertissement dans un journal que
# personne ne lit laisserait tourner une machine mal placee. Le controle est
# facultatif — sans la variable, rien n'est verifie.
if [ -n "${ABO_TAILSCALE_EXPECTED_ACCOUNT:-}" ]; then
    # **Le compte n'est lisible qu'une fois le demon `Running`.** Au premier
    # enrolement il l'est deja, parce que `tailscale up` a attendu la
    # validation ; **a la reprise depuis l'etat, non** — l'adresse revient du
    # volume avant que la table des comptes soit peuplee. La premiere version
    # de ce controle tombait donc dans cette course et annoncait « controle non
    # effectue », c'est-a-dire une degradation silencieuse : exactement ce que
    # ce controle existe pour ne pas etre.
    #
    # On attend donc, puis on **refuse**. Un controle qui ne tourne pas vaut
    # moins que pas de controle : il fait croire qu'il a tourne.
    attendu=0
    while [ "$attendu" -lt "${ABO_MESH_ACCOUNT_TIMEOUT:-30}" ]; do
        COMPTE=$(compte_de_validation)
        [ -n "$COMPTE" ] && break
        attendu=$((attendu + 2))
        sleep 2
    done

    if [ -z "$COMPTE" ]; then
        python3 /srv/mesh_announce.py failed \
            "compte de validation illisible apres ${attendu}s" || true
        echec "compte de validation illisible apres ${attendu}s.
  Le controle ABO_TAILSCALE_EXPECTED_ACCOUNT ne peut pas s'effectuer, et le
  laisser passer ferait croire qu'il a tourne. Relancer, ou retirer la variable
  si le controle n'est pas voulu."
    elif [ "$COMPTE" != "$ABO_TAILSCALE_EXPECTED_ACCOUNT" ]; then
        python3 /srv/mesh_announce.py failed \
            "valide par « ${COMPTE} », attendu « ${ABO_TAILSCALE_EXPECTED_ACCOUNT} »" || true
        echec "ce noeud a ete valide par « ${COMPTE} », attendu « ${ABO_TAILSCALE_EXPECTED_ACCOUNT} ».
  Le maillage est le perimetre de confiance de la ferme : un noeud dans le
  mauvais tailnet n'est pas un detail de configuration.
  Retirer la machine dans la console Tailscale, supprimer le volume d'etat
  (\`docker volume rm deploy_mesh_state\`), et recommencer."
    else
        echo "abo-entrypoint: valide par ${COMPTE}"
    fi
else
    # Sans controle demande, une **seule** lecture et pas de boucle : le compte
    # n'est plus qu'une information pour la console, et faire attendre trente
    # secondes une machine qui n'a rien demande serait payer un confort au prix
    # d'un demarrage.
    COMPTE=$(compte_de_validation)
fi

echo "abo-entrypoint: sur le maillage, adresse ${ADRESSE}"

# La console peut cesser d'afficher « en attente » (`ABOB-157`). Sans demande en
# attente, le backend repond `NOTHING_PENDING` et ne fabrique rien : c'est ce
# qui permet de l'appeler a chaque demarrage, y compris quand rien ne s'est
# enrole. Et si l'annonce precedente s'etait perdue, celle-ci la rattrape.
python3 /srv/mesh_announce.py settled "$ADRESSE" "$COMPTE" || true

# L'agent la lira pour n'ecouter que la (`ABOB-158`). Aujourd'hui il n'ecoute
# sur rien, et la variable est deja juste : c'est ce qui evitera d'inventer une
# seconde source de verite le jour ou il ecoutera.
export ABO_MESH_ADDRESS="$ADRESSE"

exec "$@"
