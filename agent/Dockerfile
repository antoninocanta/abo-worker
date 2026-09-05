# L'agent ABO, et **son noeud de maillage**.
#
# Volontairement minuscule cote calcul : il ne porte aucun modele, aucun poids
# et aucune dependance de calcul. Ce qui calcule vit dans les conteneurs de
# moteurs, joignables sur le reseau interne du compose.
#
# **Le maillage vit ici et nulle part ailleurs** (`ADR-015` § 1, `ADR-016` § 2).
# Une machine, un agent, une identite, un noeud. Deux raisons, et la seconde est
# celle qui interdit d'en discuter :
#
#   - un worker est une frontiere de contention, pas de capacite : sur 8 Go de
#     VRAM un seul jeu de poids tient, et l'agent mono-fil protege la carte par
#     construction. Un noeud par moteur obligerait le backend a reapprendre que
#     deux workers partagent une carte ;
#   - **les images de moteur doivent rester ignorantes du maillage.** La meme
#     image tourne sur une capacite louee, qui ne doit surtout pas rejoindre le
#     maillage (`ADR-015` § 5). Y cuire un client de maillage casserait
#     exactement la reutilisation dont le chemin `PROXY` depend.
FROM python:3.12-slim

# Tailscale, epingle par **version et empreinte**. Un tag mobile n'est pas
# retelecharge par une machine qui l'a deja en cache : elle sert l'ancien
# binaire sans que rien ne le signale. Empreintes relevees le 05/09 sur
# `pkgs.tailscale.com`, pour les deux architectures qu'on deploie.
ARG TAILSCALE_VERSION=1.102.3
ARG TARGETARCH=amd64

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) empreinte=36ddd9b51be57ffc2990cf76323cfa13643bfbb1b8a969f6183fa164741cdef5 ;; \
      arm64) empreinte=a0fa1b154af8c61f862a2259f559f7396d96c0225f4a863eae2333e1546bbe25 ;; \
      *) echo "architecture non prise en charge : ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    # `iptables` n'est pas decoratif : en mode noyau, tailscaled pose ses
    # regles de filtrage lui-meme et refuse de demarrer sans.
    apt-get install -y --no-install-recommends ca-certificates curl iptables iproute2; \
    rm -rf /var/lib/apt/lists/*; \
    archive="tailscale_${TAILSCALE_VERSION}_${TARGETARCH}"; \
    curl -fsSL -o /tmp/ts.tgz "https://pkgs.tailscale.com/stable/${archive}.tgz"; \
    echo "${empreinte}  /tmp/ts.tgz" | sha256sum -c -; \
    tar -xzf /tmp/ts.tgz -C /tmp; \
    mv "/tmp/${archive}/tailscale" /usr/local/bin/tailscale; \
    mv "/tmp/${archive}/tailscaled" /usr/local/sbin/tailscaled; \
    rm -rf /tmp/ts.tgz "/tmp/${archive}"

WORKDIR /srv

RUN pip install --no-cache-dir "httpx>=0.27,<1"

COPY agent.py .
COPY entrypoint.sh /usr/local/bin/abo-entrypoint
RUN chmod +x /usr/local/bin/abo-entrypoint

# L'etat du noeud. Un noeud ephemere s'en passerait, mais le garder evite de
# rejouer une authentification a chaque redemarrage du conteneur.
VOLUME ["/var/lib/tailscale"]

# Aucun port publie sur l'hote : ce qui ecoutera un jour (`ABOB-158`) ecoutera
# **sur l'adresse de maillage et sur aucune autre**.
ENTRYPOINT ["/usr/local/bin/abo-entrypoint"]
CMD ["python", "-u", "agent.py"]
