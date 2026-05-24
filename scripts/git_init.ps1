# Respawn Radio — Init git + remote + premier commit
# Usage : .\scripts\git_init.ps1
# (à lancer depuis la racine du projet : C:\Users\lucas\Claude-Workspace\respawn.radio)

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/s4cvl/respawn.radio.git"
$UserName = "s4cvl"
$UserEmail = "lucasbethmont@gmail.com"

# Nettoie un éventuel .git/ corrompu créé par Cowork
if (Test-Path ".git") {
    Write-Host "Suppression de l'ancien .git/..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force ".git"
}

Write-Host "git init (branche main)..." -ForegroundColor Cyan
git init -b main

Write-Host "Config user/email locale..." -ForegroundColor Cyan
git config user.name  "$UserName"
git config user.email "$UserEmail"

Write-Host "Ajout du remote origin -> $RepoUrl" -ForegroundColor Cyan
git remote add origin $RepoUrl

Write-Host "Stage de tous les fichiers..." -ForegroundColor Cyan
git add -A

Write-Host "Premier commit..." -ForegroundColor Cyan
git commit -m "feat: initial project structure

- Discord music bot (Respawn Radio)
- discord.py 2.x + yt-dlp + spotipy + FFmpeg
- Cogs: music (play/search/queue/loop/lyrics/volume) + admin
- Core: GuildPlayer (queue/loop/auto-leave), Resolver (YouTube/Spotify/search)
- Utils: embeds (palette orange) + helpers
- Docker + docker-compose (network proxy externe)
- Lyrics via lyrics.ovh (gratuit, sans cle)"

Write-Host ""
Write-Host "OK. Pour pousser sur GitHub :" -ForegroundColor Green
Write-Host "    git push -u origin main" -ForegroundColor Green
