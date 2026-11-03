# 📻 Respawn.Radio

Bot Discord de musique auto-hébergé pour le serveur **RESPAWN**. Lecture depuis YouTube et Spotify, file d'attente, panneau de contrôle à boutons. Mono-serveur, commandes préfixe `!`.

## Fonctionnalités

- Lecture depuis une URL YouTube (vidéo ou playlist), une URL Spotify (titre, playlist, album) ou une recherche texte libre
- Spotify → YouTube : les métadonnées Spotify servent à retrouver le titre sur YouTube (pas de streaming Spotify direct)
- File d'attente avec pagination, boucle titre / boucle file, mélange
- Panneau de contrôle à boutons dans un salon dédié (les autres messages y sont nettoyés automatiquement)
- Normalisation du volume (`dynaudnorm`)
- Auto-leave après 5 min seul dans le salon
- Heartbeat + watchdog de liveness, healthcheck Docker

## Stack

Python 3.12 · [py-cord](https://github.com/Pycord-Development/pycord) (voix chiffrée DAVE via `davey`) · yt-dlp + Deno · FFmpeg · spotipy · Docker Compose

## Installation

```bash
git clone git@github.com:s4cvl/respawn.radio.git
cd respawn.radio
cp .env.example .env
nano .env          # DISCORD_TOKEN, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
docker compose up -d --build
docker compose logs -f
```

### Prérequis côté Discord

Dans le [Developer Portal](https://discord.com/developers/applications) → ton application → **Bot** → **Privileged Gateway Intents**, activer :

- **Message Content Intent** (commandes préfixe)
- **Server Members Intent** (requis pour la session vocale chiffrée DAVE)

### Prérequis côté Spotify

Créer une app sur le [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) et récupérer le Client ID et le Client Secret.

## Commandes principales

| Commande | Rôle |
|---|---|
| `!play <url\|recherche>` / `!p` | Joue ou ajoute à la file |
| `!search <recherche>` | 5 résultats YouTube, choix par réaction |
| `!skip` · `!pause` · `!resume` · `!stop` | Contrôle de lecture |
| `!queue` / `!q` | Affiche la file |
| `!loop` · `!loopqueue` · `!shuffle` | Boucles et mélange |
| `!volume <0-100>` | Volume |
| `!join` · `!leave` | Salon vocal |
| `!help` | Liste complète des commandes |

## Dépendances figées

`py-cord` et `davey` sont volontairement épinglés dans `requirements.txt` : une mise à jour non testée peut casser la voix (son muet ou erreur `4017`). Mettre à jour **une lib à la fois**, rebuild, tester un `!play`.

`yt-dlp` reste en `>=` : YouTube casse régulièrement l'extraction, la dernière version est généralement nécessaire.

## Dépannage

| Symptôme | Piste |
|---|---|
| Recherche OK mais pas de lecture, `403` dans les logs | Mettre à jour yt-dlp, redémarrer le conteneur |
| `ConnectionClosed ... 4017` | Version de py-cord sans support DAVE (< 2.8) |
| Cercle vert sur le bot mais aucun son | Intent Server Members désactivé, ou version de `davey` incompatible |
| `Already connected to a voice channel` | Connexion vocale fantôme : `docker restart respawn-radio` |
