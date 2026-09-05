# Le contrat d'un moteur

Un moteur ne sait rien d'ABO. Il ne connaît ni compte, ni job, ni Abollard :
il reçoit de l'audio ou du texte, il en rend. C'est l'agent qui parle au
backend, et c'est ce partage qui permet de remplacer un moteur sans toucher au
reste (`specs/17`).

Un moteur écoute sur `127.0.0.1` ou sur le réseau interne du compose. Il
**n'ouvre jamais rien vers Internet** et n'a besoin d'aucun secret ABO.

## Les règles qui valent pour tous

**L'audio circule en base64 ou par référence signée, jamais en fichier.**
L'agent et le moteur peuvent vivre dans deux conteneurs ; un chemin partagé
serait un couplage de plus.

**C'est la distance qui choisit la forme** (`ADR-016` § 4), et l'agent décide
seul — un moteur accepte les deux et ne sait pas laquelle il recevra.

| le moteur est… | ce qu'il reçoit | pourquoi |
|---|---|---|
| dans le même compose | `<clé>_b64` | un réseau Docker ne coûte rien, et le moteur reste **sans accès sortant** |
| de l'autre côté d'Internet | `<clé>_url` + `<clé>_sha256` | relayer ferait porter **13,4 Mo par minute d'audio** à la ligne montante d'un portable domestique, alors qu'un `PROXY` est censé n'être qu'un pilote |

La règle est **les octets prennent le chemin le plus court**. Garder le base64
en local n'est pas une tiédeur : c'est ce qui préserve l'isolement d'un moteur
qui n'a aucune raison de joindre R2.

Un moteur qui reçoit `<clé>_url` :

- suit l'adresse **telle quelle**, sans ajouter le moindre en-tête. Un
  `x-amz-*` non signé fait refuser toute la requête par R2, `403`, et l'oubli
  ne se rattrape nulle part plus loin ;
- **vérifie `<clé>_sha256`**. Sur la forme base64 c'est l'agent qui confrontait
  ce que le stockage rendait à ce qui était annoncé ; en passant une adresse on
  lui retire ce contrôle, donc le moteur le reprend. Sans ça la garantie
  disparaîtrait en silence, et un résultat calculé sur la mauvaise matière ne se
  voit dans aucun format de fichier ;
- refuse les deux formes à la fois, et refuse une adresse qui n'est pas en
  `https`.

`aboengine.source()` fait les trois. Deux moteurs restent volontairement en
base64 seul : **`deepfilternet`**, qui n'a ni torch ni socle partagé — c'est ce
qui lui permet de tenir sur une machine sans carte — et qu'on ne loue jamais
puisqu'il est la capacité locale bon marché ; et **`qwen3_tts`**, dont l'entrée
lourde est un profil de voix servi par le cache de la machine et non par le
bail.

**Et la sortie ne revient pas non plus, quand `defer_output` est posé.** Même
raison, sens inverse : un `PROXY` pilote, il ne relaie pas. Le moteur garde son
résultat et ne rend que de quoi le désigner.

| `defer_output` | ce que la route rend |
|---|---|
| absent ou faux | `audio_b64`, comme avant |
| **vrai** | `output_id`, `size_bytes`, `sha256` — **aucun octet** |

Le porteur fait alors signer une concession sur **ces valeurs exactes**, ce qui
laisse `ADR-011` et `ADR-013` intacts : les octets existent au moment de signer,
ils sont juste ailleurs. Puis il rappelle `/upload` **dans la même session
Vast**, qui épingle le conteneur — donc le fichier est là.

**Le contrôle du silence passe avant.** Une sortie muette n'obtient jamais
d'`output_id` : le porteur n'a rien à déposer, plutôt que de découvrir le vide
après avoir fait signer une concession.

### `POST /upload` — déposer ce qui a été gardé

```json
{"output_id": "…32 hexa…", "put_url": "https://…", "headers": {"content-length": "…"}}
```

Suit la concession **telle quelle**, sans ajouter le moindre en-tête, puis
oublie le temporaire. Rend `{"deposited": true, "status", "size_bytes"}`.

| refus | ce qu'il veut dire |
|---|---|
| `422` | `output_id` inconnu, déjà déposé, ou adresse qui n'est pas en `https` — rejouer ailleurs ne changera rien |
| `502` | le stockage a refusé ou n'a pas répondu. **Le temporaire est conservé** : le porteur peut redemander une concession et rejouer sans qu'un chapitre de trois minutes soit recalculé |

`output_id` revient du réseau et n'est accepté que s'il est **exactement** l'un
des nôtres — 32 hexadécimaux. Sans cette borne, un `../` ferait déposer ou
effacer n'importe quel fichier du conteneur, et le porteur n'est pas forcément
celui qui a généré l'identifiant.

### `POST /drop` — le filet

Même corps, `output_id` seul. Oublie une sortie sans la déposer, pour le cas où
`/upload` n'arrive jamais. Idempotent.

`aboengine.register_deposit_routes(app)` installe les deux, identiques sur tous
les moteurs. Un moteur servi en serverless doit **aussi** les déclarer dans son
`HandlerConfig`, sinon le PyWorker ne les relaie pas — le moteur les exposerait
et l'échec arriverait chez un client sans que rien ne l'explique.

**Les clés sont en `snake_case`.** Le backend et l'agent parlent camelCase entre
eux ; à partir de l'agent, on descend en snake_case. La frontière est nette, et
c'est l'agent qui traduit.

**Un refus est un code HTTP, pas un `200` avec un champ d'erreur.** L'agent
distingue « cette machine ne sait pas faire » d'« elle a échoué » : le premier
renvoie le travail à la ferme tout de suite, le second compte une tentative.

| code | ce que l'agent en fait |
|---|---|
| `200` | résultat accepté |
| `409` | cas prévu et nommé (`VOICE_NOT_CACHED`) — l'agent corrige et rejoue |
| `422` | entrée invalide — échec, la reprise ailleurs ne changera rien |
| `501` | cette image ne porte pas cette capacité — le backend retentera ailleurs |
| `502`, `504` | le moteur a échoué — une tentative de plus est comptée |

**Un moteur vérifie ce qu'il rend.** Un fichier de la bonne durée peut ne
porter que du souffle : quatre campagnes de mesure l'ont montré sur ce projet.
Rendre du silence est un échec (`502`), pas un résultat.

**`config` vient de la route, jamais de la machine.** Le backend résout quelle
version de modèle exécute un travail et joint le réglage de sa route. C'est ce
qui permet à deux versions du même binaire de rendre deux résultats sans deux
images — et ce qui empêche un PC de choisir sa propre qualité.

## `GET /health`

Répond toujours, et dit la vérité sur le moteur — pas seulement sur le serveur
HTTP. Un agent qui rejoint la ferme déclare ce qu'il sait faire ; répondre sain
sans vérifier ferait entrer une machine qui échouera au premier travail réel.

```json
{"status": "ok", "engine": true, "enginePath": "deepfilternet3"}
```

## Les cinq opérations

### `POST /synthesize` — `TTS`

```json
{"text": "...", "language": "French", "instruction": "", "emotion": "",
 "preset_voice": "", "voice_sha256": "", "voice_b64": "<absent d'ordinaire>"}
```

Seule l'**empreinte** de la voix arrive : renvoyer 24 Mo à chaque segment d'un
chapitre serait absurde. Si la machine n'a pas ce profil, le moteur répond
`409 VOICE_NOT_CACHED` au lieu de deviner, l'agent va le chercher une fois et
rejoue avec `voice_b64`.

Rend `{"audio_b64", "format", "size_bytes", "engine"}`.

### `POST /enroll` — `VOICE_CLONE`

```json
{"reference_b64": "...", "voice_name": "...", "language": "French",
 "reference_text": "..."}
```

Rend `{"voice_b64", "sha256", "size_bytes"}`. L'empreinte rendue ne fait pas
autorité : **le backend la recalcule**. Accepter le mot de la machine
reviendrait à lui laisser nommer l'objet.

### `POST /design` — `VOICE_DESIGN`

```json
{"description": "...", "text": "...", "language": "French"}
```

Rend un extrait — `{"audio_b64", "format", "size_bytes"}` — et **jamais** un
profil durable. L'extrait validé devient ensuite l'échantillon d'un clonage.

### `POST /enhance` — `AUDIO_ENHANCE`

```json
{"audio_b64": "...", "config": {"atten_lim_db": 30}}
```

Rend `{"audio_b64", "format", "size_bytes", "engine"}`, plus les mesures que
l'agent remonte en télémétrie : `peak`, `silence_ratio`, `duration_seconds`.

Trois moteurs servent cette opération et ne se distinguent que par leur version
de modèle côté ABO. Le client demande `AUDIO_ENHANCE`, jamais « DeepFilterNet ».

| moteur | ce qu'il fait | où il tourne | mesuré |
|---|---|---|---|
| `deepfilternet` | filtre le bruit, ne touche pas à la voix | processeur | 31 dB, 94 % de parole, 4 s |
| `clearervoice` | rehausse en 48 kHz, reste fidèle | GPU | 85 dB, 97 % de parole, 4 s |
| `resemble_enhance` | **régénère** la parole | GPU | 59 dB, ~100 % de parole, 21 s |

Le troisième mérite un avertissement : il reconstruit la voix au lieu de la
filtrer, donc il peut s'écarter de ce qui a été enregistré. C'est un outil
différent des deux autres, pas une qualité supérieure — et la mesure le dit
sans ambiguïté, puisqu'il est le seul dont la parole ressort à environ 100 % au
lieu de 94 ou 97 : les filtres ne peuvent que perdre du signal, celui-ci en
ajoute.

Il porte deux comportements sur la même image, choisis par la route :
`mode = "denoise"` retire le bruit sans régénérer, `mode = "enhance"` débruite
puis reconstruit. `nfe` règle le nombre de pas du solveur — plus haut est plus
propre et coûte proportionnellement.

**Un moteur n'est pas forcément déterministe, et cela se déclare.**
`deepfilternet` rend deux fois le même octet ; `clearervoice` varie de façon
inaudible (non-déterminisme GPU) ; `resemble_enhance` varie **de façon
audible**, parce que la diffusion échantillonne au hasard. Une reprise après
panne ne rejoue donc pas le même audio pour les deux derniers.

**Le moteur normalise son entrée lui-même.** Les modèles travaillent en 48 kHz
mono ; une prise de téléphone en 16 kHz stéréo donnerait un résultat plausible
et faux — le genre de défaut qui ne se voit dans aucun format de fichier et ne
s'entend qu'à l'écoute.

### `POST /convert` — `PERFORMANCE_TRANSFER`

```json
{"audio_b64": "<la performance>", "reference_b64": "<le timbre>", "config": {}}
```

**Deux entrées de natures opposées, et les intervertir ne lève aucune erreur** :
le format serait valide, la durée juste, et le résultat serait la mauvaise voix.
`audio_b64` porte le jeu — le rythme, l'intention, les respirations —
`reference_b64` porte le timbre à lui prêter.

La référence est un **échantillon audio**, jamais un `.qvoice` : ce moteur ne
parle pas le format de Qwen. Quand le timbre vient d'une voix ABO, c'est son
échantillon d'origine qui part — précisément ce pour quoi `ADR-004` exige qu'il
survive à l'artefact.

Rend `{"audio_b64", "format", "size_bytes", "engine"}`.

## Ajouter un moteur

1. Écrire le serveur : `/health` et l'endpoint de son opération.
2. Le déclarer côté backend — une `model_version` et une `inference_route` en
   `execution = FARM`, dont l'`adapter_type` est la clé du moteur.
3. Le semer **caché** : `catalog_visibility = HIDDEN`, `serving_status =
   SERVING`. Le modèle existe et est relançable, il n'est proposé pour aucun
   nouveau travail.
4. L'éprouver sur une vraie machine, avec une vraie entrée, et **écouter** ce
   qui sort.
5. Le publier alors seulement :

```bash
docker compose -p abo_backend exec api \
  python -m app.registry.cli publish audio.deepfilternet 1 DEFAULT "ce qu'on a mesuré"
```

L'ordre compte. Publier d'abord, c'est annoncer un service qu'on n'a pas
entendu — le défaut exact que la refonte Android a trouvé sur
`PERFORMANCE_TRANSFER` et `AUDIO_ENHANCE`, dont les seules routes étaient un
bouchon `echo` qui rendait du vide.
