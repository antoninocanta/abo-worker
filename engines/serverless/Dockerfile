# check=skip=InvalidDefaultArgInFrom
# Variante serverless mince d'un moteur audio sans etat.
#
# Le moteur et ses poids viennent de l'image publiee du meme commit. Cette
# couche ajoute seulement le proxy officiel Vast et son demarrage TLS.
# Pas de valeur par defaut : une base mobile ou choisie implicitement casserait
# l'identite de modele. Le workflow passe toujours le tag du commit courant.
ARG ENGINE_IMAGE
FROM ${ENGINE_IMAGE}

ARG VASTAI_VERSION=1.6.0
RUN apt-get update && apt-get install -y --no-install-recommends curl openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "vastai==${VASTAI_VERSION}"

COPY engines/serverless/worker.py /opt/abo/serverless-worker.py
COPY engines/serverless/entrypoint.sh /opt/abo/serverless-entrypoint.sh
RUN chmod +x /opt/abo/serverless-entrypoint.sh \
    && touch /var/log/abo-engine.log

ENV WORKER_PORT=3000 \
    REPORT_ADDR=https://run.vast.ai \
    USE_SSL=true

EXPOSE 3000
ENTRYPOINT ["/opt/abo/serverless-entrypoint.sh"]
CMD []
