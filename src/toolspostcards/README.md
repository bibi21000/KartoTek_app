# Kartotek_vault

Serveur de clés API distantes, conçu pour un **redémarrage 100% autonome**
(aucune intervention humaine requise, y compris après un reboot du serveur).

## Pourquoi pas HashiCorp Vault ?

La première version de ce projet utilisait Vault. Mais le modèle de sécurité
de Vault repose sur le **scellement** (Shamir's Secret Sharing) : par design,
une instance de Vault ne doit jamais pouvoir se déchiffrer toute seule sans
qu'un humain (ou un KMS cloud externe) intervienne. C'est une excellente
propriété de sécurité — mais elle est **incompatible** avec l'exigence
« redémarre tout seul, sans aucune dépendance externe et sans aucune saisie
manuelle », qui était la contrainte de ce projet (déploiement 100%
on-premise, sans cloud).

Ce projet utilise donc un stockage chiffré plus simple, où la clé de
chiffrement est chargée directement depuis le disque au démarrage.

**Compromis assumé** : il n'y a plus de séparation des pouvoirs (personne
n'a besoin de taper une clé au démarrage, mais personne ne peut non plus
bloquer un déchiffrement non autorisé si la clé et la base sont toutes
les deux compromises). La sécurité repose entièrement sur :
- les permissions du système de fichiers (`chmod 600`, propriétaire = l'UID
  dédié `10001` du container, jamais `root`),
- idéalement un disque chiffré (LUKS) sur le serveur,
- des sauvegardes chiffrées et **stockées séparément** de la clé maîtresse.

## Architecture

```
[ Service client ] --X-API-Key--> [ kartotek-vault-api :8000 ]
                                        (FastAPI, scopes)
                                             |
                                   déchiffre avec master.key
                                             |
                                   [ secrets.db (SQLite, chiffré) ]
```

Un seul container Docker, une seule unité systemd, `Restart=always`.
Aucune notion de "seal", aucun second service à orchestrer.

## Arborescence

```
Kartotek_vault/
├── api/
│   ├── app/
│   │   ├── main.py         # routes FastAPI
│   │   ├── auth.py         # authentification par clé API + scopes
│   │   ├── crypto.py       # chiffrement Fernet, charge la clé au démarrage
│   │   ├── store.py        # stockage SQLite + journal d'audit minimal
│   │   ├── config.py
│   │   └── models.py
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── api.env.example
│   └── api_clients.json.example
├── systemd/
│   └── kartotek-vault-api.service
├── scripts/
│   ├── build.sh
│   ├── generate_master_key.py
│   ├── rotate_master_key.py
│   └── generate_api_key.py
└── README.md
```

## Installation

### 1. Préparer les répertoires sur le serveur

```bash
sudo mkdir -p /opt/kartotek_vault/{secrets,data}
sudo chown -R 10001:10001 /opt/kartotek_vault/secrets /opt/kartotek_vault/data
sudo chmod 700 /opt/kartotek_vault/secrets /opt/kartotek_vault/data
```

L'UID/GID `10001` correspond à l'utilisateur non-root `kartotek` **fixé
dans le Dockerfile** — le container ne tourne jamais en root, donc les
fichiers et répertoires montés depuis l'hôte doivent lui appartenir pour
qu'il puisse les lire/écrire. Si vous reconstruisez l'image en changeant
cet UID, refaites le `chown` en conséquence.

### 2. Générer la clé maîtresse

```bash
sudo python3 scripts/generate_master_key.py /opt/kartotek_vault/secrets/master.key
sudo chown 10001:10001 /opt/kartotek_vault/secrets/master.key
```

`sudo` crée le fichier avec `root` comme propriétaire : le `chown` qui suit
est indispensable pour que le container (UID `10001`, jamais root) puisse
le lire.

Le script écrit directement le fichier avec les permissions `600`.
**Sauvegardez une copie de ce fichier hors du serveur** (coffre-fort
d'équipe, gestionnaire de secrets séparé...) — sans lui, les secrets
stockés sont définitivement illisibles.

> **Pas de Python sur l'hôte ?** Vous pouvez utiliser l'image Docker déjà
> construite (elle contient Python + `cryptography`) au lieu d'installer
> Python sur le serveur :
> ```bash
> docker run --rm \
>   -v "$(pwd)/scripts:/scripts:ro" \
>   -v /opt/kartotek_vault/secrets:/secrets \
>   kartotek_vault-api:latest \
>   python /scripts/generate_master_key.py /secrets/master.key
> ```
> Plus besoin de `--user root` : `/opt/kartotek_vault/secrets` appartient
> déjà à l'UID `10001` (étape 1), qui est celui du container.

### 3. Déclarer les clients autorisés

```bash
sudo cp api/api_clients.json.example /opt/kartotek_vault/secrets/api_clients.json
sudo chown 10001:10001 /opt/kartotek_vault/secrets/api_clients.json
python3 scripts/generate_api_key.py
```

> **Variante Docker** (script sans dépendance externe, n'écrit rien sur
> disque, aucun souci de permissions) :
> ```bash
> docker run --rm \
>   -v "$(pwd)/scripts:/scripts:ro" \
>   kartotek_vault-api:latest \
>   python /scripts/generate_api_key.py
> ```

→ donnez la **clé brute** au client, mettez le **hash SHA256** affiché
dans `/opt/kartotek_vault/secrets/api_clients.json`, avec les scopes
voulus (`read:secrets`, `write:secrets`). Répétez pour chaque client.

### 4. Construire l'image et configurer l'environnement

```bash
bash scripts/build.sh
sudo cp api/api.env.example /opt/kartotek_vault/api.env
```

### 5. Installer et démarrer le service

```bash
sudo cp systemd/kartotek-vault-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kartotek-vault-api.service
```

C'est tout. Un `reboot`, un crash du container, une mise à jour de l'hôte :
le service revient seul, sans aucune saisie.

## Utilisation

```bash
# Écrire un secret
curl -X PUT http://127.0.0.1:8000/v1/secrets/stripe-prod \
  -H "X-API-Key: <clé_brute>" \
  -H "Content-Type: application/json" \
  -d '{"value": "sk_live_xxx", "metadata": {"owner": "team-billing"}}'

# Lire un secret
curl http://127.0.0.1:8000/v1/secrets/stripe-prod \
  -H "X-API-Key: <clé_brute>"
```

Documentation interactive : `http://127.0.0.1:8000/docs`

**`/docs`, `/redoc` et `/openapi.json` exigent désormais une clé API**, comme
le reste de l'API — un visiteur sans clé obtient une erreur, jamais la doc :

```bash
# Doc Swagger (HTML)
curl http://127.0.0.1:8000/docs \
  -H "X-API-Key: <clé_brute>" \
  -o docs.html

# Spécification OpenAPI brute (JSON)
curl http://127.0.0.1:8000/openapi.json \
  -H "X-API-Key: <clé_brute>"

# Doc ReDoc (HTML)
curl http://127.0.0.1:8000/redoc \
  -H "X-API-Key: <clé_brute>" \
  -o redoc.html
```

> **Limite connue** : Swagger UI charge `/openapi.json` via JavaScript
> depuis le navigateur, sans pouvoir y injecter automatiquement le header
> `X-API-Key`. Ouvrir `docs.html` directement dans un navigateur affichera
> donc la page mais échouera à charger le schéma. Pour une exploration
> interactive complète (bouton "Try it out"), utilisez `curl` comme
> ci-dessus, ou une extension navigateur permettant d'ajouter un header
> personnalisé à toutes les requêtes du domaine.

## Rotation de la clé maîtresse

```bash
sudo systemctl stop kartotek-vault-api.service
sudo python3 scripts/generate_master_key.py /opt/kartotek_vault/secrets/master.key.new
sudo python3 scripts/rotate_master_key.py \
    --db /opt/kartotek_vault/data/secrets.db \
    --old-key /opt/kartotek_vault/secrets/master.key \
    --new-key /opt/kartotek_vault/secrets/master.key.new
sudo chown 10001:10001 /opt/kartotek_vault/secrets/master.key /opt/kartotek_vault/secrets/master.key.bak
sudo systemctl start kartotek-vault-api.service
```

Là encore, tout ce qui passe par `sudo` (génération de la nouvelle clé,
sauvegarde `.bak` créée par le script de rotation) se retrouve owned par
`root` : le `chown` final est nécessaire avant de redémarrer le service.

> **Variante Docker** :
> ```bash
> sudo systemctl stop kartotek-vault-api.service
>
> docker run --rm \
>   -v "$(pwd)/scripts:/scripts:ro" \
>   -v /opt/kartotek_vault/secrets:/secrets \
>   kartotek_vault-api:latest \
>   python /scripts/generate_master_key.py /secrets/master.key.new
>
> docker run --rm \
>   -v "$(pwd)/scripts:/scripts:ro" \
>   -v /opt/kartotek_vault/secrets:/secrets \
>   -v /opt/kartotek_vault/data:/data \
>   kartotek_vault-api:latest \
>   python /scripts/rotate_master_key.py \
>     --db /data/secrets.db \
>     --old-key /secrets/master.key \
>     --new-key /secrets/master.key.new
>
> sudo systemctl start kartotek-vault-api.service
> ```

## Sécurité — points d'attention

- **Jamais** de clé API en clair côté serveur (seul le hash SHA256 figure
  dans `api_clients.json`), jamais de log de secret.
- `master.key` et `secrets.db` ne doivent **jamais** être sauvegardés
  ensemble au même endroit : une sauvegarde qui contient les deux annule
  toute la protection.
- Activez le chiffrement de disque (LUKS) sur `/opt/kartotek_vault` en
  complément — cela protège contre le vol physique du disque ou d'une
  image de VM.
- Limitez strictement l'accès SSH/root à ce serveur : quiconque a un accès
  root peut lire `master.key`.
- Le journal d'audit (table `audit_log` dans `secrets.db`) trace qui a
  lu/écrit/supprimé quel secret et quand — pensez à l'exporter
  régulièrement vers un système de logs centralisé en lecture seule.

## Si un accès cloud devient possible un jour

Si vous obtenez un jour un accès, même minime, à AWS/GCP/Azure, il vaut la
peine de revenir vers HashiCorp Vault avec **auto-unseal via KMS cloud** :
vous récupérez alors la séparation des pouvoirs et la gestion fine
(rotation, baux, versioning) de Vault, tout en gardant un redémarrage
100% automatique — les deux contraintes deviennent compatibles dès qu'un
KMS externe entre en jeu.
