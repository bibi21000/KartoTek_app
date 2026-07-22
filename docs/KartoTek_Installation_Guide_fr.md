# Guide d'installation de KartoTek

Ce guide décrit la procédure complète d'installation de **KartoTek** (paquet PyPI `pypostcards`), sous **Linux**, **Windows** et **macOS**.

L'application fournit quatre outils :

| Outil       | Rôle                                            | Type |
|-------------|--------------------------------------------------|------|
| `ktmanager` | Gestion et navigation dans la collection de cartes | GUI |
| `ktimport`  | Import / validation des cartes numérisées        | GUI |
| `ktscan`    | Numérisation par lots                            | GUI |
| `kttools`   | Outils en ligne de commande (dont `publish`)     | CLI |

---

## 1. Prérequis

### 1.1 Communs aux deux systèmes

- **Python 3.9 ou supérieur** (avec les modules `venv` et `pip`)
- Une **connexion Internet** (le script installe le paquet depuis PyPI)
- Selon la version choisie à l'installation :
  - **Version légère** : `ktmanager`, `ktimport` uniquement
  - **Version complète** : ajoute l'OCR (reconnaissance de texte), la recherche par similarité et le module voyages — nécessite **Tesseract OCR**
- Un **scanner** installé si vous comptez utiliser `ktscan`

### 1.2 Spécifique à Linux

- Une distribution basée sur **apt** (Debian/Ubuntu), **dnf** (Fedora), **pacman** (Arch) ou **zypper** (openSUSE) — le script détecte automatiquement le gestionnaire de paquets et peut proposer d'installer les outils manquants via `sudo`
- Pour la numérisation : **sane-utils** (ou équivalent selon la distribution)

### 1.3 Spécifique à Windows

- **Windows 10 (21H2 ou supérieur) ou Windows 11**, qui intègrent **winget** (App Installer) nativement. Sur une version plus ancienne, installez winget depuis le [Microsoft Store](https://apps.microsoft.com/detail/9nblggh4nns1) ou passez par une installation manuelle de Python/Tesseract.
- **PowerShell 5.1** ou supérieur (préinstallé sur Windows 10/11)
- Pour la numérisation : le pilote **WIA/TWAIN** fourni par le fabricant de votre scanner (pas d'équivalent de sane-utils à installer — Windows gère cela nativement une fois le pilote du scanner en place)
- Droits administrateur **recommandés mais pas obligatoires** : ils ne sont utiles que si winget doit installer Python ou Tesseract pour vous. Le reste de l'installation (venv, lanceurs, raccourcis) se fait entièrement dans votre profil utilisateur, sans droits élevés.

### 1.4 Spécifique à macOS

- **macOS 12 (Monterey) ou supérieur** recommandé
- **[Homebrew](https://brew.sh)** doit être installé au préalable — le script s'appuie dessus pour installer Python, Tesseract et les outils de numérisation. S'il est absent, le script vous indique l'URL d'installation et s'arrête (il n'exécute jamais lui-même la commande d'installation de Homebrew, par prudence).
- Les outils Apple **`sips`** et **`iconutil`** (préinstallés sur tout macOS) sont utilisés pour générer l'icône des applications ; aucune action de votre part n'est nécessaire.
- Pour la numérisation : la formule Homebrew **sane-backends**, ou à défaut l'application **Transfert d'images** (Image Capture) fournie par Apple selon votre scanner.
- Aucun droit administrateur n'est requis : Homebrew et l'installation elle-même (venv, lanceurs, applications) fonctionnent entièrement dans votre profil utilisateur.

---

## 2. Installation sous Linux

### 2.1 Récupérer le script

Placez `install_KartoTek.sh` dans un dossier de votre choix, puis rendez-le exécutable :

```bash
chmod +x install_KartoTek.sh
```

### 2.2 Lancer l'installation

```bash
./install_KartoTek.sh
```

ou explicitement :

```bash
./install_KartoTek.sh --install
```

Le script va, dans l'ordre :

1. Vérifier la présence de `python3`, du module `venv` et de `pip` (et proposer de les installer via le gestionnaire de paquets détecté si besoin) ;
2. Vous demander la version à installer : **légère** ou **complète** ;
3. (version complète) Vérifier/proposer d'installer **Tesseract** et les langues OCR souhaitées ;
4. Vérifier/proposer d'installer **scanimage** (sane-utils), utile pour `ktscan` ;
5. Créer un environnement virtuel Python dans `~/.local/share/pypostcards/venv` et y installer le paquet `pypostcards` depuis PyPI ;
6. Créer le fichier de configuration `~/.local/share/pypostcards/postcards.conf` (s'il n'existe pas déjà) ;
7. Créer les lanceurs dans `~/.local/bin` ;
8. Créer les fichiers `.desktop` (menu applications) et récupérer une icône si le paquet en fournit une.

En fin d'installation, si `~/.local/bin` n'est pas déjà dans votre `PATH`, le script vous indique la ligne à ajouter à votre `~/.bashrc` (ou équivalent) :

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 2.3 Autres commandes

```bash
./install_KartoTek.sh --update              # met à jour uniquement KartoTek
./install_KartoTek.sh --update-complete      # met aussi à jour tous les paquets du venv
./install_KartoTek.sh --uninstall            # désinstalle KartoTek
./install_KartoTek.sh --version              # affiche la version du script
./install_KartoTek.sh --help                 # affiche l'aide
```

---

## 3. Installation sous Windows

### 3.1 Récupérer le script

Téléchargez `install_KartoTek.ps1` et placez-le dans un dossier de votre choix (par exemple `C:\Users\VotreNom\Downloads`).

### 3.2 Autoriser l'exécution du script (une seule fois)

Par défaut, Windows bloque l'exécution des scripts PowerShell téléchargés. Ouvrez **PowerShell** (pas besoin d'administrateur) et exécutez :

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Cette commande n'autorise l'exécution de scripts que pour la fenêtre PowerShell en cours ; elle ne modifie pas la configuration globale de votre machine.

### 3.3 Lancer l'installation

Placez-vous dans le dossier contenant le script, puis :

```powershell
cd C:\Users\VotreNom\Downloads
.\install_KartoTek.ps1
```

ou explicitement :

```powershell
.\install_KartoTek.ps1 -Install
```

Le script va, dans l'ordre :

1. Vérifier la présence de Python 3 (`py -3` ou `python`), du module `venv` et de `pip` — et proposer de les installer via **winget** si besoin (Python.Python.3.12) ;
2. Vous demander la version à installer : **légère** ou **complète** ;
3. (version complète) Vérifier/proposer d'installer **Tesseract OCR** via winget (UB-Mannheim.TesseractOCR) et vous rappeler que le choix des langues se fait pendant l'installeur Tesseract lui-même ;
4. Vous rappeler que la numérisation sous Windows repose sur le pilote WIA/TWAIN de votre scanner (rien à installer via ce script) ;
5. Créer un environnement virtuel Python dans `%LOCALAPPDATA%\PyPostcards\venv` et y installer le paquet `pypostcards` depuis PyPI ;
6. Créer le fichier de configuration `%LOCALAPPDATA%\PyPostcards\postcards.conf` (s'il n'existe pas déjà) ;
7. Créer les lanceurs (`.cmd`) dans `%LOCALAPPDATA%\PyPostcards\bin` et ajouter ce dossier à votre `PATH` utilisateur ;
8. Créer les raccourcis dans le menu Démarrer (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\KartoTek`).

> **Important** : après une première installation, le `PATH` utilisateur vient d'être modifié. **Fermez et rouvrez** votre fenêtre PowerShell (ou reconnectez-vous) pour que les commandes `ktmanager`, `ktimport`, `ktscan` et `kttools` soient reconnues directement dans un nouveau terminal. En attendant, vous pouvez aussi lancer les outils depuis le menu Démarrer.

### 3.4 Autres commandes

```powershell
.\install_KartoTek.ps1 -Update            # met à jour uniquement KartoTek
.\install_KartoTek.ps1 -UpdateComplete    # met aussi à jour tous les paquets du venv
.\install_KartoTek.ps1 -Uninstall         # désinstalle KartoTek
.\install_KartoTek.ps1 -Version           # affiche la version du script
.\install_KartoTek.ps1 -Help              # affiche l'aide
```

---

## 4. Installation sous macOS

### 4.1 Installer Homebrew (si ce n'est pas déjà fait)

Ouvrez l'application **Terminal** et vérifiez si Homebrew est présent :

```bash
brew --version
```

S'il n'est pas installé, rendez-vous sur [https://brew.sh](https://brew.sh) et suivez les instructions officielles (une seule ligne de commande à copier-coller). Une fois Homebrew installé, revenez à l'étape suivante.

### 4.2 Récupérer le script

Placez `install_KartoTek_macos.sh` dans un dossier de votre choix, puis rendez-le exécutable :

```bash
chmod +x install_KartoTek_macos.sh
```

### 4.3 Lancer l'installation

```bash
./install_KartoTek_macos.sh
```

ou explicitement :

```bash
./install_KartoTek_macos.sh --install
```

Le script va, dans l'ordre :

1. Vérifier la présence de `python3`, du module `venv` et de `pip` (et proposer de les installer via **Homebrew** — formule `python@3.12` — si besoin ; si Homebrew est absent, le script affiche l'URL d'installation et s'arrête) ;
2. Vous demander la version à installer : **légère** ou **complète** ;
3. (version complète) Vérifier/proposer d'installer **Tesseract** (`brew install tesseract`), puis vérifier si les langues souhaitées (français, anglais) sont présentes. Homebrew ne permet pas de choisir une langue à la fois : si des langues manquent, le script propose d'installer la formule **`tesseract-lang`**, qui ajoute toutes les langues supplémentaires en une seule fois (téléchargement volumineux) ;
4. Vérifier/proposer d'installer **sane-backends** (`scanimage`), utile pour `ktscan` — en notant que, selon le modèle de scanner, l'application **Transfert d'images** d'Apple peut être une alternative ;
5. Créer un environnement virtuel Python dans `~/Library/Application Support/PyPostcards/venv` et y installer le paquet `pypostcards` depuis PyPI ;
6. Créer le fichier de configuration `~/Library/Application Support/PyPostcards/postcards.conf` (s'il n'existe pas déjà) ;
7. Créer les lanceurs shell dans `~/.local/bin` ;
8. Créer une icône `.icns` (à partir de l'icône fournie par le paquet, via `sips`/`iconutil`) puis générer des applications **`.app`** minimales dans `~/Applications/KartoTek`, visibles depuis **Launchpad** et **Spotlight**.

En fin d'installation, si `~/.local/bin` n'est pas déjà dans votre `PATH`, le script vous indique la ligne à ajouter à votre `~/.zshrc` (shell par défaut depuis macOS Catalina) :

```bash
export PATH="$HOME/.local/bin:$PATH"
```

> **Gatekeeper** : la première fois que vous ouvrez une des applications créées (`.app`), macOS peut afficher un avertissement « développeur non identifié » car elles ne sont pas signées par un compte développeur Apple. Faites un **clic droit → Ouvrir** une première fois sur l'application concernée ; les ouvertures suivantes se feront normalement par double-clic.

### 4.4 Autres commandes

```bash
./install_KartoTek_macos.sh --update              # met à jour uniquement KartoTek
./install_KartoTek_macos.sh --update-complete      # met aussi à jour tous les paquets du venv
./install_KartoTek_macos.sh --uninstall            # désinstalle KartoTek
./install_KartoTek_macos.sh --version              # affiche la version du script
./install_KartoTek_macos.sh --help                 # affiche l'aide
```

---

## 5. Emplacements des fichiers

| Élément                        | Linux                                         | Windows                                                        | macOS                                                             |
|---------------------------------|------------------------------------------------|------------------------------------------------------------------|---------------------------------------------------------------------|
| Environnement virtuel (venv)    | `~/.local/share/pypostcards/venv`              | `%LOCALAPPDATA%\PyPostcards\venv`                                | `~/Library/Application Support/PyPostcards/venv`                    |
| Fichier de configuration        | `~/.local/share/pypostcards/postcards.conf`    | `%LOCALAPPDATA%\PyPostcards\postcards.conf`                      | `~/Library/Application Support/PyPostcards/postcards.conf`          |
| Lanceurs                        | `~/.local/bin`                                 | `%LOCALAPPDATA%\PyPostcards\bin`                                 | `~/.local/bin`                                                       |
| Icônes                          | `~/.local/share/icons/pypostcards`             | `%LOCALAPPDATA%\PyPostcards\icons`                               | `~/Library/Application Support/PyPostcards/icons`                   |
| Raccourcis / menu applications  | `~/.local/share/applications` (`.desktop`)     | Menu Démarrer → dossier `KartoTek` (`.lnk`)                       | `~/Applications/KartoTek` (`.app`, visibles dans Launchpad)          |
| Données (cartes, images)        | `~/KartoTek/{data,import,tmp,logs}`            | `%USERPROFILE%\KartoTek\{data,import,tmp,logs}`                  | `~/KartoTek/{data,import,tmp,logs}`                                  |

Ces chemins de données sont définis dans le fichier `postcards.conf` (section `[DEFAULT]`) et peuvent être modifiés manuellement à tout moment ; les dossiers correspondants sont recréés automatiquement s'ils manquent, aussi bien à l'installation qu'à chaque mise à jour.

---

## 6. Différences notables entre les trois versions du script

- **Gestionnaire de paquets système** : `apt`/`dnf`/`pacman`/`zypper` + `sudo` sous Linux, **winget** sous Windows, **Homebrew** sous macOS. Sur Windows comme sur macOS, l'essentiel de l'installation ne nécessite pas de droits administrateur.
- **Langues Tesseract** : sous Linux, chaque langue s'installe comme un paquet système séparé (le script les détecte et les installe une à une). Sous Windows, l'installeur officiel de Tesseract propose un écran de sélection des langues au moment de l'installation. Sous macOS, Homebrew ne permet pas de choisir une langue à la fois : la formule `tesseract-lang` installe toutes les langues supplémentaires en une fois.
- **Numérisation** : sous Linux et macOS, `ktscan` peut s'appuyer sur **SANE** (`scanimage`, paquet `sane-utils`/`sane-backends`) que le script peut installer. Sous Windows, la numérisation passe par les pilotes **WIA/TWAIN** du fabricant du scanner ; il n'y a rien d'équivalent à installer via ce script.
- **Raccourcis d'application** : fichiers `.desktop` sous Linux, raccourcis `.lnk` dans le menu Démarrer sous Windows, applications `.app` minimales dans `~/Applications` sous macOS (avec icône `.icns` générée automatiquement quand c'est possible).
- **PATH** : sous Linux et macOS, le script affiche la ligne à ajouter manuellement à votre fichier de démarrage shell (`~/.bashrc`, `~/.zshrc`, ...). Sous Windows, le script modifie directement la variable d'environnement `PATH` de votre utilisateur (aucune édition manuelle nécessaire, un redémarrage du terminal suffit).

---

## 7. Dépannage

**« L'exécution de scripts est désactivée sur ce système » (Windows)**
→ Exécutez `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` dans la fenêtre PowerShell avant de relancer le script.

**Les commandes `ktmanager`, `ktimport`, etc. restent introuvables après l'installation**
→ Ouvrez une nouvelle fenêtre de terminal (le `PATH` n'est pris en compte que par les nouvelles sessions), ou utilisez les raccourcis créés (menu Démarrer sous Windows, menu applications sous Linux, Launchpad sous macOS).

**Erreur liée à winget introuvable (Windows)**
→ Mettez à jour « App Installer » depuis le Microsoft Store, ou installez Python et Tesseract manuellement depuis leurs sites officiels, puis relancez le script : il détectera les outils déjà présents et sautera cette étape.

**Homebrew introuvable (macOS)**
→ Installez-le depuis [https://brew.sh](https://brew.sh), fermez puis rouvrez le Terminal (pour que `brew` soit reconnu dans le `PATH`), puis relancez le script.

**« développeur non identifié » en ouvrant une application KartoTek (macOS)**
→ Clic droit (ou Ctrl-clic) sur l'application dans `~/Applications/KartoTek` puis « Ouvrir », une seule fois.

**OCR non fonctionnel après l'installation**
→ Vérifiez que Tesseract est bien dans le `PATH` système (`tesseract --version` doit fonctionner dans un terminal) et que la langue voulue est installée (`tesseract --list-langs`).
