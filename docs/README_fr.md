L'installation s'effectue dans un environnement virtuel Python.

Deux types d'installation sont proposés : légère ou complète.
La version complète nécessite plusieurs gigaoctets d'espace disque mais permet :

- la recherche par similarité : saisissez l'URL de l'image d'une carte postale et l'application recherchera des cartes similaires dans votre collection
- la recherche de doublons
- le calcul de visites virtuelles à partir de coordonnées GPS
- la reconnaissance optique de caractères (OCR)


### Installation sous Linux

Télécharger le script **install_KartoTek.sh** et exécutez le :

> chmod 755 install_KartoTek.sh

> ./install_KartoTek.sh


### Installation sous Windows

Téléchargez **install_KartoTek.ps1** et placez-le dans un dossier de votre choix (par exemple `C:\Users\VotreNom\Downloads`).

Par défaut, Windows bloque l'exécution des scripts PowerShell téléchargés. Ouvrez **PowerShell** (pas besoin d'administrateur) et exécutez :

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```
Placez-vous dans le dossier contenant le script, puis :

```powershell
cd C:\Users\YourName\Downloads
.\install_KartoTek.ps1
```

### Installation sous macOS

Placez **install_KartoTek_macos.sh** dans un dossier de votre choix, puis rendez-le exécutable et lancez le:

```bash
chmod +x install_KartoTek_macos.sh
./install_KartoTek_macos.sh
```
