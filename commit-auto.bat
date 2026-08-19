@echo off
REM Commit + push automatique pour PronosticApp-Backend

REM Vérifier qu'on est dans un repo git
git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
    echo Erreur : ce dossier n'est pas un depot Git.
    pause
    exit /b 1
)

REM Ajouter tous les changements
git add .

REM Commit avec message fixe (à modifier si tu veux)
set MSG=Commit automatique - %date% %time%
echo Commit avec le message : %MSG%
git commit -m "%MSG%"

if errorlevel 1 (
    echo Rien a committer ou echec du commit.
    pause
    exit /b 1
)

REM Push vers origin main
git push -u origin main

echo Termine.
pause