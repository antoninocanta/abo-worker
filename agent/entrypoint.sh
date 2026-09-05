#!/bin/sh
# Rejoindre le maillage, **puis** lancer l'agent — jamais l'inverse.
#
# `ADR-015` § 1 : le maillage est la condition d'existence d'un worker. Une
# machine qui n'a pas d'adresse de maillage n'est pas un worker degrade, elle
# n'est pas un worker. Cet entrypoint refuse donc de lancer l'agent plutot que
# de le laisser s'enroler par le tunnel public : ce serait exactement la dette
# que le realignement retire, et elle serait invisible.
#
# Mode **noyau**, avec `/dev/net/tun` (arbitre par Onin le 05/09). Le mode
# utilisateur reste a instruire ; l'objectif « installation en une passe » tient
# parce que le device et les capacites se declarent dans le compose.
set -eu

MAILLAGE_ETAT=/var/lib/tailscale/tailscaled.state
MAILLAGE_SOCKET=/run/tailscale/tailscaled.sock
ATTENTE_MAX=${ABO_MESH_READY_TIMEOUT:-60}

echec() {
    echo "abo-entrypoint: $1" >&2
    exit 1
}

# --- Ce que le conteneur doit avoir recu -------------------------------------
#
# On le verifie ici et pas dans l'agent : ces trois manques sont des defauts de
# **deploiement**, et les diagnostiquer depuis un journal d'agent coute une
# location. `ABOB-155` a paye ce defaut une fois — la machine tournait, notre
# code n'existait pas, et rien ne disait pourquoi.

[ -c /dev/net/tun ] || echec "/dev/net/tun absent. Le compose doit porter :
    devices:  [ \"/dev/net/tun:/dev/net/tun\" ]
    cap_add:  [ NET_ADMIN, NET_RAW ]
  Sans ce device, tailscaled ne peut pas creer d'interface en mode noyau."

if [ -z "${ABO_TAILSCALE_AUTHKEY:-}" ] && [ ! -s "$MAILLAGE_ETAT" ]; then
    echec "ABO_TAILSCALE_AUTHKEY est requis au premier demarrage.
  Recette d'enrolement dans documentation/exploitation.md : une cle
  reutilisable, ephemere et taguee, pour n'avoir ni secret par machine a
  distribuer ni noeud mort a nettoyer."
fi

# --- Le noeud ----------------------------------------------------------------

mkdir -p /run/tailscale /var/lib/tailscale
tailscaled --state="$MAILLAGE_ETAT" --socket="$MAILLAGE_SOCKET" --tun=tailscale0 &
MAILLAGE_PID=$!

# Un `tailscale up` lance trop tot echoue sur la socket, pas sur le reseau : on
# attend que le demon reponde avant de conclure quoi que ce soit.
attendu=0
while [ ! -S "$MAILLAGE_SOCKET" ]; do
    attendu=$((attendu + 1))
    [ "$attendu" -lt "$ATTENTE_MAX" ] || echec "tailscaled n'a pas ouvert sa socket en ${ATTENTE_MAX}s."
    kill -0 "$MAILLAGE_PID" 2>/dev/null || echec "tailscaled s'est arrete au demarrage."
    sleep 1
done

# **Un nom de noeud est un label DNS, et une cle de machine n'en est pas un.**
# Les cles sont de la forme `wk_ab12cd`, et l'underscore est refuse :
# « "abo-wk_epreuve" is not a valid DNS label ». Sans cette traduction, *aucune*
# machine ne pourrait s'enroler — mesure du 05/09, sur la premiere epreuve.
NOM_NOEUD="abo-$(printf '%s' "${ABO_WORKER_KEY:-sans-cle}" | tr '_' '-' | tr -cd 'a-zA-Z0-9-')"

# `--accept-dns=false` a dessein : reecrire le resolv.conf du conteneur casserait
# la resolution de ce qui est **hors** du maillage — R2, Vast, les fournisseurs
# de langue — et le maillage ne couvre que le lien machine <-> backend
# (`ADR-015` § 1). On adresse donc le backend explicitement.
#
# `--hostname` porte la cle de la machine : un noeud anonyme dans une console de
# maillage ne se revoque pas, faute de savoir lequel c'est.
tailscale --socket="$MAILLAGE_SOCKET" up \
    --accept-dns=false \
    --hostname="$NOM_NOEUD" \
    ${ABO_TAILSCALE_AUTHKEY:+--authkey="${ABO_TAILSCALE_AUTHKEY}"} \
    ${ABO_TAILSCALE_LOGIN_SERVER:+--login-server="${ABO_TAILSCALE_LOGIN_SERVER}"} \
    ${ABO_TAILSCALE_TAGS:+--advertise-tags="${ABO_TAILSCALE_TAGS}"} \
    || echec "l'enrolement au maillage a echoue (noeud « ${NOM_NOEUD} »).
  **La raison exacte est dans la ligne que tailscale vient d'ecrire au-dessus**,
  et elle n'est pas toujours la cle : un nom de noeud invalide, un tag que la
  cle ne porte pas et une cle expiree echouent tous ici. Ne pas deviner."

ADRESSE=$(tailscale --socket="$MAILLAGE_SOCKET" ip -4 2>/dev/null | head -n 1 || true)
[ -n "$ADRESSE" ] || echec "aucune adresse de maillage obtenue. Un worker hors maillage n'est pas un worker (ADR-015 § 1)."

echo "abo-entrypoint: sur le maillage, adresse ${ADRESSE}"

# L'agent la lira pour n'ecouter que la (`ABOB-158`). Aujourd'hui il n'ecoute
# sur rien, et la variable est deja juste : c'est ce qui evitera d'inventer une
# seconde source de verite le jour ou il ecoutera.
export ABO_MESH_ADDRESS="$ADRESSE"

exec "$@"
