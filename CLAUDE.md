# CLAUDE.md — discord-music-bot

> Fichier de contexte projet pour Claude / Cowork. À placer à la racine du repo.

---

## Vue d'ensemble

Bot Discord musique "maison" en Python, déployé sur **SRV-DEB** (Dell Optiplex 3020M, Debian 13, Docker). Usage privé — un seul serveur Discord. Pas de sharding, pas de multi-guild.

**Objectif principal** : permettre à un groupe d'amis de streamer de la musique dans un salon vocal Discord via des commandes préfixe `!`.

---

## Stack technique

| Composant | Choix | Rôle |
|---|---|---|
| Langage | Python 3.12 | Runtime principal |
| Framework Discord | `discord.py` v2.x | Connexion bot + events |
| Audio | `yt-dlp` + `FFmpeg` | Extraction flux + streaming vocal |
| Spotify | `spotipy` | Résolution metadata Spotify → YouTube |
| File d'attente | In-memory (liste Python) | Queue par guild |
| Conteneurisation | Docker + Compose | Déploiement SRV-DEB |
| Config | `.env` (python-dotenv) | Secrets hors code |

**Flow Spotify** : `lien/recherche Spotify` → `spotipy` récupère titre + artiste → recherche YouTube via `yt-dlp` → stream FFmpeg. Le streaming audio Spotify direct n'est pas utilisé (verrouillé côté API).

---

## Structure du projet

```
discord-music-bot/
├── bot/
│   ├── __init__.py
│   ├── main.py              # Entrypoint, setup bot + cogs
│   ├── cogs/
│   │   ├── music.py         # Toutes les commandes musique
│   │   └── admin.py         # Commandes admin optionnelles
│   ├── core/
│   │   ├── player.py        # Logique player + queue par guild
│   │   ├── resolver.py      # Résolution URLs (YouTube, Spotify, recherche)
│   │   └── spotify.py       # Wrapper spotipy
│   └── utils/
│       ├── embeds.py        # Builders d'embeds Discord
│       └── helpers.py       # Fonctions utilitaires
├── .env                     # Secrets (jamais commité)
├── .env.example             # Template secrets
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── CLAUDE.md                # Ce fichier
```

---

## Variables d'environnement

Fichier `.env` à la racine (ne jamais commiter) :

```env
# Discord
DISCORD_TOKEN=

# Spotify API (app Developer sur compte perso — gratuit)
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=

# Optionnel
COMMAND_PREFIX=!
LOG_LEVEL=INFO
```

`.env.example` doit toujours être à jour avec les clés (sans valeurs).

---

## Commandes disponibles

Préfixe : `!`

### Lecture

| Commande | Description |
|---|---|
| `!play <url\|nom>` | Ajoute à la queue et lance. Accepte : URL YouTube, playlist YouTube, URL Spotify track, URL Spotify playlist, ou recherche texte libre |
| `!search <query>` | Affiche les 5 premiers résultats YouTube, l'utilisateur choisit via réaction |
| `!pause` | Met en pause |
| `!resume` | Reprend après pause |
| `!stop` | Arrête la lecture et vide la queue |
| `!skip` | Passe à la piste suivante |
| `!skipto <position>` | Saute directement à la position N dans la queue |

### Queue

| Commande | Description |
|---|---|
| `!queue` / `!q` | Affiche la queue avec pagination (10 titres par page) |
| `!remove <position>` | Supprime la piste à la position N |
| `!clear` | Vide la queue sans stopper la lecture en cours |
| `!shuffle` | Mélange aléatoirement la queue |
| `!move <de> <vers>` | Déplace une piste de la position A à la position B |

### Infos

| Commande | Description |
|---|---|
| `!nowplaying` / `!np` | Affiche la piste en cours avec barre de progression |
| `!lyrics` | Affiche les paroles de la piste en cours (via API externe si dispo) |

### Salon vocal

| Commande | Description |
|---|---|
| `!join` | Le bot rejoint le salon vocal de l'utilisateur |
| `!leave` | Le bot quitte et vide la queue |
| `!volume <0-100>` | Ajuste le volume (défaut : 50) |

### Boucle

| Commande | Description |
|---|---|
| `!loop` | Active/désactive la boucle sur la piste en cours |
| `!loopqueue` | Active/désactive la boucle sur toute la queue |

---

## Résolution des sources (`resolver.py`)

Le resolver doit gérer ces cas dans l'ordre :

1. **URL YouTube track** (`youtube.com/watch?v=`, `youtu.be/`) → extraction directe yt-dlp
2. **URL YouTube playlist** (`youtube.com/playlist?list=`) → extraction de toutes les pistes, ajout en batch à la queue
3. **URL Spotify track** (`open.spotify.com/track/`) → spotipy metadata → recherche YouTube → yt-dlp
4. **URL Spotify playlist** (`open.spotify.com/playlist/`) → spotipy liste toutes les tracks → pour chacune : recherche YouTube → ajout queue
5. **Texte libre** → recherche `ytsearch:<query>` via yt-dlp, prend le premier résultat

Pour les playlists (YouTube ou Spotify), informer l'utilisateur via embed : `"✅ X pistes ajoutées à la queue"`.

---

## Architecture Player (`player.py`)

- Une instance `GuildPlayer` par guild (stockée dans un dict `players: dict[int, GuildPlayer]`)
- Chaque `GuildPlayer` contient :
  - `queue: list[Track]` — liste des pistes en attente
  - `current: Track | None` — piste en cours
  - `loop: bool` — boucle piste
  - `loop_queue: bool` — boucle queue
  - `volume: float` — volume (0.0 à 1.0)
  - `voice_client: discord.VoiceClient`
- Nettoyage automatique : si le bot est seul dans le salon vocal depuis 5 minutes → `!leave` automatique

---

## Embeds Discord (`embeds.py`)

Tous les messages du bot passent par des embeds cohérents. Style sombre, accent orange.

- **Now Playing** : titre, artiste, durée, barre de progression ASCII, thumbnail, demandé par
- **Queue** : liste numérotée, durée totale en footer, pagination avec réactions ◀️ ▶️
- **Track ajoutée** : titre, position dans la queue, durée
- **Erreur** : embed rouge avec message clair
- **Succès** : embed vert (confirmations légères)

---

## Docker

### `Dockerfile`

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY .env .

CMD ["python", "-m", "bot.main"]
```

### `docker-compose.yml`

```yaml
services:
  discord-music-bot:
    build: .
    container_name: discord-music-bot
    restart: unless-stopped
    env_file: .env
    networks:
      - proxy

networks:
  proxy:
    external: true
```

Le service rejoint le réseau Docker `proxy` existant sur SRV-DEB. Aucun port exposé nécessaire.

---

## `requirements.txt`

```
discord.py>=2.3.0
yt-dlp>=2024.1.0
spotipy>=2.23.0
python-dotenv>=1.0.0
PyNaCl>=1.5.0
aiohttp>=3.9.0
```

> `PyNaCl` est requis pour le support audio vocal de discord.py.

---

## Conventions de code

- **Style** : PEP 8, type hints partout, docstrings sur les fonctions publiques
- **Async** : tout ce qui touche Discord ou les I/O réseau est `async`/`await`
- **Erreurs** : toujours catcher les exceptions dans les commandes et renvoyer un embed erreur à l'utilisateur plutôt que de faire crasher le bot
- **Logs** : utiliser `logging` Python standard, niveau configurable via `LOG_LEVEL` dans `.env`
- **Pas de commandes slash** : uniquement préfixe `!` pour ce projet

---

## Comportements attendus

- Si l'utilisateur n'est pas dans un salon vocal lors d'un `!play` → message d'erreur, ne pas rejoindre
- Si le bot est déjà dans un salon différent → message d'erreur
- Les playlists Spotify longues (> 50 tracks) sont chargées en stream : les premières pistes sont jouables pendant que le reste se résout en background
- Le volume par défaut est 50 % au démarrage
- En cas d'erreur de résolution d'une piste (track indisponible, privée…) → skip automatique + message dans le canal

---

## Ce qui est hors scope

- Multi-serveur / sharding
- Dashboard web
- Système de permissions avancé
- Base de données persistante (historique, playlists sauvegardées)
- Streaming Spotify direct (non supporté par l'API)
- Commandes slash Discord

---

## Setup initial (référence rapide)

```bash
# 1. Cloner le repo sur SRV-DEB
git clone <repo> ~/discord-music-bot && cd ~/discord-music-bot

# 2. Copier et remplir les secrets
cp .env.example .env && nano .env

# 3. Build et lancer
docker compose up -d --build

# 4. Vérifier les logs
docker compose logs -f
```

Créer l'app Spotify Developer sur [developer.spotify.com](https://developer.spotify.com/dashboard) → récupérer `Client ID` et `Client Secret`. Redirect URI : `http://localhost:8888/callback` (non utilisée en pratique pour ce bot).
