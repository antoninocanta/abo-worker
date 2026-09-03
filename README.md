# abo-worker

Unité de calcul d'ABO. Un PC équipé d'un GPU devient un worker : on l'installe,
il se déclare, il reçoit du travail.

Statut : **l'agent existe et tire du travail** ; le moteur Qwen3-TTS est
éprouvé sur GPU.

## L'idée

Le calcul n'est pas un fournisseur, c'est une **ferme de machines**. Un PC
personnel, un serveur loué, une instance Vast.ai à la demande : toutes font
tourner le même agent et parlent le même protocole.

```text
ABO Control Plane  →  Compute Pool  →  workers
                                       ├─ PC perso (RTX 3090)
                                       ├─ serveur distant
                                       └─ Vast.ai, pour le débordement
```

Acheter demain un PC d'occasion doit vouloir dire : brancher, installer,
rejoindre la ferme. Vast.ai devient le **turbo à la demande**, pas le cœur de
l'architecture.

## Deux briques

| | Rôle |
|---|---|
| **agent** | se déclare au backend, envoie son pouls, tire du travail, rend le résultat |
| **moteurs** | conteneurs qui savent faire une chose : synthèse, clonage, nettoyage |

L'agent ne sait rien des modèles ; un moteur ne sait rien d'ABO. Entre les
deux, un contrat HTTP local sur `127.0.0.1`.

## Connexion sortante uniquement

Un worker **n'ouvre aucun port**. Il compose vers le backend en HTTPS et tire
son travail. C'est ce qui rend un PC derrière une box domestique utilisable
sans toucher au routeur, et c'est ce qui évite d'exposer une machine
personnelle sur Internet.

L'administration passe par un VPN de type Tailscale — pour s'y connecter à la
main, pas pour faire circuler les jobs.

## Répertoires

```text
abo_worker/
  agent/      l'agent ABO : s'enrôle, tire du travail, rend le résultat
  engines/    un répertoire par moteur, plus le contrat qu'ils suivent tous
    CONTRACT.md      ce qu'un moteur doit servir, et ce qu'il doit vérifier
    base/            socle commun aux moteurs qui ont besoin de torch
    qwen3_tts/       synthèse, clonage, voice design
    deepfilternet/   nettoyage de bruit, sur processeur
    clearervoice/    rehaussement 48 kHz, sur GPU
    chatterbox/      transfert de jeu (« Follow My Lead »)
  deploy/     installation d'une machine et lancement des conteneurs
  docs/       ce qui est propre au worker
  .github/    construction et publication des images
```

## Ce qu'une machine porte, elle le choisit

Les moteurs sont derrière des **profils** compose. Une machine sans carte ne
lance que ce qui tourne sur processeur ; une machine qui en a une choisit ce
qu'elle veut servir. Déclarer dans `ABO_ENGINES` un moteur qu'on ne fait pas
tourner fait entrer dans la ferme une machine qui échouera au premier travail
d'un utilisateur.

```bash
docker compose --profile enhance-cpu up -d                 # nettoyage, sans GPU
docker compose --profile tts --profile transfer up -d      # synthèse + jeu
```

## Les poids sont dans les images

Depuis `ADR-009` § 9, et pour les cinq moteurs. Une machine qui tire une image
est prête à servir : il n'y a rien à monter, rien à préparer, et rien à
télécharger au premier travail d'un utilisateur.

```bash
docker compose --profile transfer up -d
```

La règle précédente — poids montés depuis `ABO_ENGINE_DATA` — reposait sur une
**prémisse fausse** : « une image qui les porte se retélécharge en entier à
chaque correction du serveur ». `docker pull` ne récupère que les couches
**manquantes**. Les poids étant écrits sous le `COPY server.py`, corriger le
serveur ne coûte que la dernière couche, quelques kilo-octets.

Ce qui reste vrai de l'ancienne raison — la place sur la machine qui construit —
ne justifie pas de faire attendre une carte louée pendant qu'elle télécharge un
gigaoctet.

**Ne remettez pas de volume sur `/weights`.** Il masquerait les poids cuits, et
donnerait le pire des deux mondes : une image lourde *et* un téléchargement au
premier appel. `ABO_ENGINE_DATA` n'est plus lu par aucun service.

## Les images sont publiées, toutes

| image | contenu |
|---|---|
| `abo-worker-agent:0.4.0` | l'agent, 196 Mo, sans poids ni torch — `v1` pointe dessus |
| `abo-engine-base:cuda` | le socle torch partagé par trois moteurs |
| `abo-deepfilternet:v1` | nettoyage rapide, processeur, 268 Mo |
| `abo-clearervoice:v1` | rehaussement 48 kHz, GPU |
| `abo-chatterbox:v1` | transfert de jeu, GPU |
| `abo-resemble-enhance:v1` | nettoyage profond, GPU, le plus lourd |
| `abo-qwen3-tts:v1` | synthèse, clonage, conception — 31,8 Go |

Jusqu'au 03/09 le workflow ne publiait que Qwen, et `abo-engine-base` n'existait
que sur la machine de développement : les trois moteurs torch qui en descendent
étaient donc **introuvables ailleurs**. Une machine louée ne pouvait ni les
construire ni les tirer.

Qwen ne se reconstruit plus à chaque poussée — 31,8 Go pour une image qui change
rarement. Il reste à un clic, case à cocher sur « Run workflow ».

**Référencer par digest, jamais par `:v1`.** Un tag mobile n'est pas
retéléchargé par une machine qui l'a déjà en cache : elle sert l'ancienne image
sans que rien ne le signale, et ça coûte une location de GPU pour rien. Le
workflow imprime le digest de chaque image dans son résumé.

## Ce que chaque moteur fait, mesuré

Sur RTX 2060 Super, 03/09/2026, même prise de 5,6 s volontairement bruitée.
**Chaque mesure porte une vérification de contenu** — une belle durée sur du
souffle a déjà été rendue quatre fois sur ce projet.

| moteur | opération | mesuré | où | mur |
|---|---|---|---|---|
| `deepfilternet` | `AUDIO_ENHANCE` | 31 dB retirés, **94 %** de parole | processeur | 4 s |
| `clearervoice` | `AUDIO_ENHANCE` | 85 dB retirés, **97 %** de parole | GPU | 4 s |
| `resemble_enhance` | `AUDIO_ENHANCE` | 59 dB retirés, **~100 %** de parole | GPU | **21 s** |
| `chatterbox` | `PERFORMANCE_TRANSFER` | enveloppe **+0,96** avec la performance contre +0,80 avec le timbre | GPU | 3 s |

Les trois premiers servent la **même** opération à trois qualités : le client
demande `AUDIO_ENHANCE`, jamais un nom de moteur. Le chiffre qui compte pour
Chatterbox est le dernier — il dit que la sortie a gardé le **jeu** de la
performance et pas celui de la voix de référence, ce qui est toute la promesse.

Deux nuances qui décident de l'usage, et qu'aucune de ces colonnes ne dit seule.

`clearervoice` efface **toute** la pièce entre les mots (plancher de bruit à 0).
C'est plus propre et ce n'est pas forcément mieux : à écouter avant d'en faire
un défaut.

`resemble_enhance` **régénère** au lieu de filtrer, et la mesure le montre :
c'est le seul dont la parole ressort à environ 100 % au lieu de 94 ou 97. Les
filtres ne peuvent que perdre du signal ; celui-ci en **ajoute**. Pour une
prise irrécupérable c'est ce qui la sauve ; pour une prise correcte c'est un
risque sans contrepartie, et il coûte cinq fois le temps des deux autres.

### Ce que « le même travail » veut dire selon le moteur

Deux appels identiques, même entrée, empreinte de la sortie comparée :

| moteur | deux fois le même résultat | pourquoi |
|---|---|---|
| `deepfilternet` | **oui**, à l'octet | déterministe, sur processeur |
| `clearervoice` | non, mais l'écart est invisible (pic 10821 contre 10820) | non-déterminisme GPU, ordre des réductions flottantes |
| `resemble_enhance` | **non, et l'écart s'entend** (pic 9613 contre 10214, ~6 %) | la diffusion échantillonne au hasard |

Trois conséquences. Une reprise après panne ne rejoue pas le même audio pour
les deux derniers. Un utilisateur qui relance un nettoyage profond obtient
autre chose — ce qui peut être un usage (« réessaie ») mais ne doit jamais être
présenté comme une correction déterministe. Et cela donne un cas net à la
question ouverte de `specs/17` : ici, même machine, même moteur, même entrée,
et deux sorties différentes.

### Les quatre tiennent sur une carte de 8 Go, tout juste

**6,3 Go de VRAM sur 8** avec les quatre moteurs chargés, éprouvé en alternance
sur cinq travaux enchaînés — aucun échec, 3 à 5 secondes chacun. Il reste
1,8 Go, donc **y ajouter Qwen ne passe pas** : il garde un jeu de poids complet
par voix résidente (~3,9 Go en bf16 pour le 1.7B).

C'est le partage qu'une machine doit trancher : cette carte fait le traitement
audio **ou** la synthèse, pas les deux.

### Les trois tiennent ensemble sur une carte de 8 Go

Éprouvé en alternance — transfert, nettoyage GPU, nettoyage CPU, transfert,
nettoyage GPU — chaque travail rendu en 2 à 4 secondes, sortie vérifiée comme
parole à chaque fois : **2,8 Go de VRAM sur 8**, aucune tentative perdue.

C'est l'inverse de Qwen, et la différence mérite d'être connue avant de peupler
une machine : Qwen garde un jeu de poids complet **par voix résidente** (~3,9 Go
en bf16 pour le 1.7B), d'où `QWEN_MAX_RESIDENT=1` sur 8 Go. Ces trois-là gardent
un modèle chacun et n'en changent pas selon la voix. Une même machine peut donc
porter le nettoyage et le transfert sans arbitrage ; y ajouter Qwen demande de
compter.

## Le protocole fait autorité côté backend

Le contrat entre un worker et ABO — enregistrement, pouls, attribution d'un
job, remise du résultat — est spécifié dans le dépôt du backend :
`abo_backend/specs/17-workers-et-ferme-de-calcul.md`. Ce dépôt-ci l'implémente,
il ne le définit pas.

## État

- `engines/qwen3_tts` : image construite et publiée, contrat `/enroll`,
  `/synthesize`, `/design` opérationnel en local.
- **Le GPU est enfin demandé.** Sans `--backend cuda`, le moteur reste
  entièrement sur le CPU — trois mesures ont tourné sur des cartes louées et
  inactives avant qu'on le voie. Corrigé, **pas encore mesuré**.
- **Résidence des poids** : les synthèses passent par un pool de processus
  `--serve` gardés vivants, un par voix, au lieu d'une invocation par segment.
  Neuf tests sans GPU ; sur carte, elle évite un téléversement de ~3,9 Go par
  appel.
- **Moteur seul** : `ABO_ENGINE_ONLY=1` lance le serveur de modèle sans
  certificat Vast ni proxy. C'est le mode de la ferme — l'agent est un conteneur
  séparé — et celui d'un essai local, Windows et Docker Desktop compris.
- `engines/qwen3_tts/worker.py` : proxy PyWorker, **spécifique au serverless
  Vast.ai**. L'agent ABO le remplace désormais ; il ne sert plus que si l'on
  revient au serverless de Vast.
- `agent/` : **écrit et éprouvé de bout en bout**. Il s'enrôle, envoie son
  pouls, tire un job, appelle le moteur sur `127.0.0.1` et rend le résultat.
  Aucun port ouvert. `agent/tests/fake_engine.py` rend un WAV valide pour
  éprouver la chaîne sans carte.
- **L'agent suit des concessions au lieu de recevoir des octets** (0.3.0,
  `ABOB-136`, `ADR-010`). Le bail ne porte plus `audioB64` ni `referenceB64`
  mais une **référence** et une concession : où aller chercher l'entrée, où
  déposer la sortie. L'agent suit, il ne construit rien — le jour où l'adresse
  désignera un stockage objet au lieu d'une route d'ABO, ce code ne bougera pas.

  **C'est la version qui décide**, et le backend la lit : en dessous de 0.3.0 il
  sert l'ancienne forme. Une machine tirée d'une image ancienne continue donc de
  fonctionner — sur une location, échouer au premier travail coûterait l'heure
  quand même. Ne pas baisser `AGENT_VERSION` sans retirer le code qui va avec.

  **Les moteurs ne changent pas.** Ils parlent toujours JSON et base64 sur
  `127.0.0.1`, où le tiers de volume ne se paie pas ; ce qui coûtait cher était
  le même tiers sur le lien montant. Les cinq images restent telles quelles,
  seule celle de l'agent est à reconstruire.

- **Le profil de voix arrive lui aussi en concession** (0.4.0, `ABOB-137`,
  `ADR-011`). `GET /voices/{sha256}` ne rend plus 33 Mo de base64 mais une
  concession de quelques centaines d'octets ; l'agent la suit et va chercher le
  `.qvoice` **directement sur le stockage**. C'est le seul transfert du projet
  qui pèse 25 Mo, et le seul où quelqu'un attend devant l'écran.

  Mesuré sur un profil réel de 25 218 963 octets, empreinte vérifiée des deux
  côtés : **33 625 396 octets à travers le backend, contre 691**. À 7,5 Mbit/s,
  c'est 35,9 s de lien montant qui disparaissent.

  **Une porte de version distincte**, et ce n'est pas un doublon : un agent
  `0.3.0` suit déjà une concession pour l'entrée d'un job mais lit encore
  `voiceB64` pour un profil. Les servir par la même porte casserait toutes les
  machines `0.3.0` en vol.

  L'agent **vérifie l'empreinte** de ce qu'une concession lui rend. Une
  concession désigne un tiers ; la suivre sans confronter le résultat à ce qu'on
  demandait reviendrait à faire chanter au moteur une voix qu'on n'a pas
  choisie.

  **Un dépôt vers une adresse absolue ne reçoit aucun paramètre.** Une URL
  présignée signe sa propre query : y ajouter `attempt` ou `kind` invaliderait
  la signature, et le stockage répondrait `403` sans rien expliquer.

- **L'agent attend son moteur avant de s'enrôler** (0.2.0, `ABOB-128`). Il
  interroge `/health` et exige `engine: true` ; sans cela il renonce plutôt que
  de rejoindre la ferme. S'enrôler d'abord et découvrir ensuite ferait entrer
  une machine qui promet une capacité qu'elle ne sert pas — et sur une machine
  **louée**, elle reçoit le travail d'un utilisateur, échoue, et l'heure est
  facturée quand même.
- **Trois `/health` mentaient** : ils répondaient `engine: true` dès que le
  répertoire de poids existait, or l'image le crée toujours. Ils vérifient
  maintenant qu'il y a vraiment des octets dedans — `aboengine.weights_present()`.
  Une santé qui ment ne retardait pas l'échec, elle le garantissait.

```bash
cd engines/qwen3_tts && chmod +x tests/fake_qwen.py && python -m pytest tests -q
```
