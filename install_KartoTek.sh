#!/usr/bin/env bash
#
# install_pypostcards.sh
# ---------------------------------------------------------------------------
# Installe PyPostcards depuis PyPI dans un environnement virtuel Python.
# Installs PyPostcards from PyPI inside a Python virtual environment.
#
# Usage: ./install_pypostcards.sh
# ---------------------------------------------------------------------------

set -euo pipefail

# =============================================================================
# VERSION DU SCRIPT
# -----------------------------------------------------------------------------
# IMPORTANT : incrémentez le numéro MINEUR (X.Y.Z -> X.Y+1.Z) à CHAQUE
# modification de ce script, aussi petite soit-elle (nouvelle option,
# correction, nouveau message, etc.). Le numéro MAJEUR est réservé aux
# changements de comportement importants ou aux ruptures de compatibilité.
# IMPORTANT: bump the MINOR version number (X.Y.Z -> X.Y+1.Z) on EVERY change
# to this script, however small (new option, fix, new message, etc.). The
# MAJOR version is reserved for significant behavior changes or breaking
# changes.
# =============================================================================
SCRIPT_VERSION="1.4.0"

# =============================================================================
# CONFIGURATION (modifiable facilement / easy to edit)
# =============================================================================

# Nom du paquet PyPI (utilisé pour `pip install`)
PACKAGE_NAME="pypostcards"

# Nom commercial de l'application (affiché à l'utilisateur, .desktop, etc.)
APP_NAME="KartoTek"

# Emplacement du venv (surchargeable via variable d'environnement)
VENV_DIR="${PYPOSTCARDS_VENV_DIR:-$HOME/.local/share/pypostcards/venv}"

# Emplacement du fichier de configuration (utilisé par tous les scripts via --conffile)
CONF_DIR="$HOME/.local/share/pypostcards"
CONF_FILE="$CONF_DIR/postcards.conf"

# Où poser les lanceurs et les .desktop
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/pypostcards"

# Scripts / entry points fournis par le paquet
declare -A ENTRY_POINTS=(
  [kttools]="tkpostcards.scripts.tktools:cli"
  [ktmanager]="tkpostcards.tkmanager:run"
  [ktimport]="tkpostcards.tkimport:run"
  [ktscan]="tkpostcards.tkscan:run"
)
# Ordre d'affichage stable
SCRIPT_ORDER=(kttools ktmanager ktimport ktscan)

# Scripts pour lesquels on crée un fichier .desktop (GUI). kttools est un CLI.
DESKTOP_SCRIPTS=(ktmanager ktimport ktscan)

# --- Langues Tesseract ------------------------------------------------------
# Langues installées PAR DEFAUT (codes tesseract : fra, eng, deu, ...)
TESSERACT_LANGS_DEFAULT=("fra" "eng")

# Langues PROPOSEES en plus à l'utilisateur (modifiez cette liste pour ajouter
# d'autres langues facilement). Le script ne proposera que celles absentes
# de TESSERACT_LANGS_DEFAULT.
TESSERACT_LANGS_AVAILABLE=("eng" "fra" "deu")

# =============================================================================
# INTERNATIONALISATION / I18N
# =============================================================================

LANG_CODE="en"
case "${LANG:-}" in
  fr*) LANG_CODE="fr" ;;
  *)   LANG_CODE="en" ;;
esac

declare -A MSG_FR=(
  [title]="=== Installation de %s (paquet PyPI : %s) ==="
  [checking_tools]="Vérification des outils système requis (python3, venv, pip)..."
  [tool_missing]="Outil manquant : %s"
  [propose_install_tools]="Certains outils système sont manquants. Les installer maintenant avec sudo ? [o/N] "
  [tools_ok]="Tous les outils système nécessaires sont présents."
  [no_pkg_manager]="Impossible de détecter un gestionnaire de paquets (apt/dnf/pacman/zypper). Installez manuellement : %s"
  [install_cancelled]="Installation annulée par l'utilisateur."
  [choose_variant]="Quelle version installer ?"
  [variant_light]="Légère (ktmanager, ktimport)"
  [variant_full]="Complète (+ ocr, similar, travel)"
  [choose_prompt]="Votre choix [1/2] : "
  [creating_venv]="Création de l'environnement virtuel dans %s ..."
  [venv_exists]="Un venv existe déjà dans %s, il sera réutilisé."
  [installing_pkg]="Installation de %s (extra : %s) depuis PyPI..."
  [checking_tesseract]="Vérification de la présence du binaire tesseract..."
  [tesseract_found]="tesseract est déjà installé (%s)."
  [tesseract_missing]="tesseract n'est pas installé mais est requis pour l'OCR."
  [propose_tesseract_install]="Installer tesseract-ocr maintenant ? [o/N] "
  [tesseract_skip_warning]="ATTENTION : sans tesseract, les fonctionnalités OCR ne fonctionneront pas."
  [ask_extra_lang]="Ajouter la langue Tesseract '%s' (%s) ? [o/N] "
  [lang_already_installed]="La langue Tesseract '%s' (%s) est déjà installée, elle est conservée automatiquement."
  [installing_lang_packs]="Installation des paquets de langues Tesseract manquants : %s"
  [no_lang_packs_to_install]="Toutes les langues Tesseract sélectionnées sont déjà installées."
  [checking_scanimage]="Vérification de la présence de scanimage (sane-utils)..."
  [scanimage_found]="scanimage est déjà installé (%s)."
  [scanimage_missing]="scanimage n'est pas installé mais est requis par ktscan."
  [propose_scanimage_install]="Installer sane-utils (scanimage) maintenant ? [o/N] "
  [scanimage_skip_warning]="ATTENTION : sans scanimage/sane-utils, ktscan ne pourra pas numériser."
  [creating_conf]="Création du fichier de configuration dans %s ..."
  [conf_exists]="Un fichier de configuration existe déjà dans %s, il est conservé tel quel."
  [creating_data_dir]="Création du dossier manquant : %s"
  [summary_conf]="Fichier de configuration : %s"
  [creating_launchers]="Création des lanceurs dans %s ..."
  [creating_desktop]="Création des fichiers .desktop dans %s ..."
  [icon_not_found]="Aucune icône trouvée dans le paquet, une icône générique sera utilisée."
  [path_reminder]="Remarque : ajoutez %s à votre PATH si ce n'est pas déjà fait :"
  [done]="Installation terminée !"
  [summary_light]="Version installée : légère (ktmanager, ktimport)"
  [summary_full]="Version installée : complète (ktmanager, ktimport, ocr, similar, travel)"
  [summary_langs]="Langues Tesseract installées : %s"
  [summary_launch]="Vous pouvez lancer les outils avec : %s"
  [yes_no_hint]="(o = oui / n = non)"
  [version_line]="%s - version du script : %s"
  [usage]="Usage : %s [--install|--update|--update-complete|--uninstall|--version|--help]"
  [help_install]="  --install               Installe (ou réinstalle) %s (option par défaut)"
  [help_update]="  --update, --upgrade     Met à jour %s vers la dernière version depuis PyPI"
  [help_update_complete]="  --update-complete, --update-all"
  [help_update_complete_note]="                          Comme --update, et met aussi à jour TOUS les paquets du venv"
  [help_uninstall]="  --uninstall             Désinstalle %s (venv, lanceurs, .desktop, icônes)"
  [help_uninstall_note]="                          Ne supprime PAS les paquets système (tesseract, sane-utils, ...)"
  [help_version]="  --version, -v           Affiche le numéro de version du script"
  [help_help]="  --help, -h              Affiche cette aide"
  [update_title]="=== Mise à jour de %s ==="
  [uninstall_title]="=== Désinstallation de %s ==="
  [update_no_install]="Aucune installation existante trouvée dans %s. Lancez d'abord : %s --install"
  [uninstall_no_install]="Aucune installation existante trouvée. Rien à désinstaller."
  [updating_pkg]="Mise à jour de %s (extra : %s) depuis PyPI..."
  [updating_all_pkgs]="Mise à jour complète : mise à jour de tous les paquets du venv (au-delà de %s)..."
  [no_outdated_pkgs]="Tous les paquets du venv sont déjà à jour."
  [update_done]="Mise à jour terminée !"
  [update_all_done]="Mise à jour complète terminée !"
  [uninstall_removing_venv]="Suppression de l'environnement virtuel : %s"
  [uninstall_removing_launchers]="Suppression des lanceurs dans %s"
  [uninstall_removing_desktop]="Suppression des fichiers .desktop dans %s"
  [uninstall_removing_icons]="Suppression des icônes dans %s"
  [uninstall_keep_pkg_note]="Note : les paquets système (tesseract, sane-utils, python3-venv, ...) installés via le gestionnaire de paquets ne sont PAS supprimés. Désinstallez-les manuellement si besoin."
  [ask_remove_conf]="Supprimer aussi le fichier de configuration (%s) ? [o/N] "
  [conf_removed]="Fichier de configuration supprimé : %s"
  [conf_kept]="Fichier de configuration conservé : %s"
  [data_dirs_kept_note]="Remarque : vos données (cartes, images, dans %s) n'ont PAS été supprimées."
  [uninstall_done]="Désinstallation terminée."
)

declare -A MSG_EN=(
  [title]="=== %s installation (PyPI package: %s) ==="
  [checking_tools]="Checking required system tools (python3, venv, pip)..."
  [tool_missing]="Missing tool: %s"
  [propose_install_tools]="Some system tools are missing. Install them now with sudo? [y/N] "
  [tools_ok]="All required system tools are present."
  [no_pkg_manager]="Could not detect a package manager (apt/dnf/pacman/zypper). Please install manually: %s"
  [install_cancelled]="Installation cancelled by user."
  [choose_variant]="Which version do you want to install?"
  [variant_light]="Light (ktmanager, ktimport)"
  [variant_full]="Full (+ ocr, similar, travel)"
  [choose_prompt]="Your choice [1/2]: "
  [creating_venv]="Creating virtual environment in %s ..."
  [venv_exists]="A venv already exists in %s, it will be reused."
  [installing_pkg]="Installing %s (extra: %s) from PyPI..."
  [checking_tesseract]="Checking for the tesseract binary..."
  [tesseract_found]="tesseract is already installed (%s)."
  [tesseract_missing]="tesseract is not installed but is required for OCR."
  [propose_tesseract_install]="Install tesseract-ocr now? [y/N] "
  [tesseract_skip_warning]="WARNING: without tesseract, OCR features will not work."
  [ask_extra_lang]="Add Tesseract language '%s' (%s)? [y/N] "
  [lang_already_installed]="Tesseract language '%s' (%s) is already installed, it is kept automatically."
  [installing_lang_packs]="Installing missing Tesseract language packs: %s"
  [no_lang_packs_to_install]="All selected Tesseract languages are already installed."
  [checking_scanimage]="Checking for scanimage (sane-utils)..."
  [scanimage_found]="scanimage is already installed (%s)."
  [scanimage_missing]="scanimage is not installed but is required by ktscan."
  [propose_scanimage_install]="Install sane-utils (scanimage) now? [y/N] "
  [scanimage_skip_warning]="WARNING: without scanimage/sane-utils, ktscan will not be able to scan."
  [creating_conf]="Creating configuration file in %s ..."
  [conf_exists]="A configuration file already exists in %s, it is kept as is."
  [creating_data_dir]="Creating missing folder: %s"
  [summary_conf]="Configuration file: %s"
  [creating_launchers]="Creating launchers in %s ..."
  [creating_desktop]="Creating .desktop files in %s ..."
  [icon_not_found]="No icon found in the package, a generic icon will be used."
  [path_reminder]="Note: add %s to your PATH if it isn't already:"
  [done]="Installation complete!"
  [summary_light]="Installed version: light (ktmanager, ktimport)"
  [summary_full]="Installed version: full (ktmanager, ktimport, ocr, similar, travel)"
  [summary_langs]="Installed Tesseract languages: %s"
  [summary_launch]="You can launch the tools with: %s"
  [yes_no_hint]="(y = yes / n = no)"
  [version_line]="%s - script version: %s"
  [usage]="Usage: %s [--install|--update|--update-complete|--uninstall|--version|--help]"
  [help_install]="  --install               Install (or reinstall) %s (default)"
  [help_update]="  --update, --upgrade     Update %s to the latest version from PyPI"
  [help_update_complete]="  --update-complete, --update-all"
  [help_update_complete_note]="                          Same as --update, and also update ALL packages in the venv"
  [help_uninstall]="  --uninstall             Uninstall %s (venv, launchers, .desktop files, icons)"
  [help_uninstall_note]="                          Does NOT remove system packages (tesseract, sane-utils, ...)"
  [help_version]="  --version, -v           Show the script version number"
  [help_help]="  --help, -h              Show this help"
  [update_title]="=== Updating %s ==="
  [uninstall_title]="=== Uninstalling %s ==="
  [update_no_install]="No existing installation found in %s. Run: %s --install first"
  [uninstall_no_install]="No existing installation found. Nothing to uninstall."
  [updating_pkg]="Updating %s (extra: %s) from PyPI..."
  [updating_all_pkgs]="Full update: updating every package in the venv (beyond %s)..."
  [no_outdated_pkgs]="All packages in the venv are already up to date."
  [update_done]="Update complete!"
  [update_all_done]="Full update complete!"
  [uninstall_removing_venv]="Removing virtual environment: %s"
  [uninstall_removing_launchers]="Removing launchers from %s"
  [uninstall_removing_desktop]="Removing .desktop files from %s"
  [uninstall_removing_icons]="Removing icons from %s"
  [uninstall_keep_pkg_note]="Note: system packages (tesseract, sane-utils, python3-venv, ...) installed via the package manager are NOT removed. Uninstall them manually if needed."
  [ask_remove_conf]="Also remove the configuration file (%s)? [y/N] "
  [conf_removed]="Configuration file removed: %s"
  [conf_kept]="Configuration file kept: %s"
  [data_dirs_kept_note]="Note: your data (cards, images, in %s) was NOT removed."
  [uninstall_done]="Uninstall complete."
)

# msg <key> [args for printf...]
msg() {
  local key="$1"; shift || true
  local fmt
  if [[ "$LANG_CODE" == "fr" ]]; then
    fmt="${MSG_FR[$key]:-$key}"
  else
    fmt="${MSG_EN[$key]:-$key}"
  fi
  # shellcheck disable=SC2059
  printf "$fmt" "$@"
}
msgln() { msg "$@"; echo; }

ask_yes_no() {
  # ask_yes_no <message-already-printed-without-newline>
  local reply
  read -r reply || true
  case "$reply" in
    [oOyY]*) return 0 ;;
    *) return 1 ;;
  esac
}

# =============================================================================
# DETECTION AUTOMATIQUE DE LA LANGUE (repli sur l'anglais)
# AUTOMATIC LANGUAGE DETECTION (fallback to English)
# =============================================================================

detect_lang() {
  local probe
  # On regarde LANGUAGE, puis LC_ALL, puis LANG, dans cet ordre de priorité.
  # LANGUAGE peut contenir une liste type "fr:en_US", on ne garde que le premier code.
  for probe in "${LANGUAGE:-}" "${LC_ALL:-}" "${LANG:-}"; do
    [[ -z "$probe" ]] && continue
    probe="${probe%%:*}"   # garde le premier élément si liste séparée par ':'
    case "$probe" in
      fr*|FR*) echo "fr"; return ;;
    esac
  done
  echo "en"
}

LANG_CODE="$(detect_lang)"

# =============================================================================
# ANALYSE DES ARGUMENTS / ARGUMENT PARSING
# =============================================================================

print_usage() {
  local prog
  prog="$(basename "$0")"
  msgln version_line "$APP_NAME" "$SCRIPT_VERSION"
  echo
  msgln usage "$prog"
  echo
  msgln help_install "$APP_NAME"
  msgln help_update "$APP_NAME"
  msgln help_update_complete
  msgln help_update_complete_note
  msgln help_uninstall "$APP_NAME"
  msgln help_uninstall_note
  msgln help_version
  msgln help_help
}

ACTION="install"
# UPDATE_MODE ne s'applique que quand ACTION="update" :
#   normal   -> met à jour uniquement $PACKAGE_NAME (comportement historique)
#   complete -> met aussi à jour TOUS les paquets installés dans le venv
UPDATE_MODE="normal"
for arg in "$@"; do
  case "$arg" in
    --install)
      ACTION="install"
      ;;
    --update|--upgrade)
      ACTION="update"
      UPDATE_MODE="normal"
      ;;
    --update-complete|--update-all|--upgrade-complete|--upgrade-all)
      ACTION="update"
      UPDATE_MODE="complete"
      ;;
    --uninstall)
      ACTION="uninstall"
      ;;
    --version|-v)
      msgln version_line "$APP_NAME" "$SCRIPT_VERSION"
      exit 0
      ;;
    --help|-h)
      print_usage
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      print_usage
      exit 1
      ;;
  esac
done

if [[ "$ACTION" == "install" ]]; then
  echo "$APP_NAME installer / Installateur $APP_NAME"
  msgln title "$APP_NAME" "$PACKAGE_NAME"
  msgln version_line "$APP_NAME" "$SCRIPT_VERSION"
  echo
fi

# =============================================================================
# DETECTION DU GESTIONNAIRE DE PAQUETS
# =============================================================================

PKG_MANAGER=""
if command -v apt-get >/dev/null 2>&1; then
  PKG_MANAGER="apt"
elif command -v dnf >/dev/null 2>&1; then
  PKG_MANAGER="dnf"
elif command -v pacman >/dev/null 2>&1; then
  PKG_MANAGER="pacman"
elif command -v zypper >/dev/null 2>&1; then
  PKG_MANAGER="zypper"
fi

PKG_LIST_UPDATED=false

pkg_update_list() {
  # Ne rafraîchit la liste des paquets qu'une seule fois par exécution du
  # script, même si pkg_install est appelé plusieurs fois.
  $PKG_LIST_UPDATED && return 0
  case "$PKG_MANAGER" in
    apt)    sudo apt-get update -y ;;
    pacman) sudo pacman -Sy --noconfirm ;;
    *) : ;;  # dnf/zypper rafraîchissent leurs métadonnées automatiquement
  esac
  PKG_LIST_UPDATED=true
}

pkg_install() {
  # pkg_install pkg1 pkg2 ...
  pkg_update_list
  case "$PKG_MANAGER" in
    apt)    sudo apt-get install -y "$@" ;;
    dnf)    sudo dnf install -y "$@" ;;
    pacman) sudo pacman -S --noconfirm "$@" ;;
    zypper) sudo zypper install -y "$@" ;;
    *) return 1 ;;
  esac
}

# Noms de paquets système par distribution --------------------------------
tesseract_bin_pkg() {
  case "$PKG_MANAGER" in
    apt)    echo "tesseract-ocr" ;;
    dnf)    echo "tesseract" ;;
    pacman) echo "tesseract" ;;
    zypper) echo "tesseract-ocr" ;;
    *) echo "tesseract" ;;
  esac
}

tesseract_lang_pkg() {
  local code="$1"
  case "$PKG_MANAGER" in
    apt)    echo "tesseract-ocr-${code}" ;;
    dnf)    echo "tesseract-langpack-${code}" ;;
    pacman) echo "tesseract-data-${code}" ;;
    zypper) echo "tesseract-ocr-traineddata-${code}" ;;
    *) echo "tesseract-ocr-${code}" ;;
  esac
}

# tesseract_lang_installed <code>
# -----------------------------------------------------------------------
# Renvoie 0 (vrai) si la langue tesseract <code> (ex: "fra") est déjà
# installée sur le système, 1 (faux) sinon. On interroge directement
# tesseract lui-même (--list-langs), qui reflète l'état réel du système,
# plutôt que de se fier uniquement à la liste TESSERACT_LANGS_DEFAULT.
#
# Returns 0 (true) if tesseract language <code> (e.g. "fra") is already
# installed on the system, 1 (false) otherwise. We query tesseract
# itself (--list-langs), which reflects the actual system state, rather
# than only relying on the TESSERACT_LANGS_DEFAULT list.
# -----------------------------------------------------------------------
tesseract_lang_installed() {
  local code="$1"
  command -v tesseract >/dev/null 2>&1 || return 1
  tesseract --list-langs 2>/dev/null | grep -qx "$code"
}

sane_utils_pkg() {
  case "$PKG_MANAGER" in
    apt)    echo "sane-utils" ;;
    dnf)    echo "sane-backends" ;;
    pacman) echo "sane" ;;
    zypper) echo "sane" ;;
    *) echo "sane-utils" ;;
  esac
}

python_venv_pkg() {
  case "$PKG_MANAGER" in
    apt)    echo "python3-venv" ;;
    dnf)    echo "" ;;      # inclus dans python3
    pacman) echo "" ;;      # inclus dans python
    zypper) echo "" ;;      # inclus dans python3
    *) echo "" ;;
  esac
}

# Extras pip (setup.cfg/pyproject "extras_require") à installer selon la
# variante choisie. "light" = fonctions de base (gestion + import).
# "full" = light + OCR + recherche de similarité + module voyages.
pip_extra_for_variant() {
  case "$1" in
    light) echo "ktmanager,ktimport" ;;
    full)  echo "ktmanager,ktimport,ocr,similar,travel" ;;
    *)     echo "ktmanager,ktimport" ;;
  esac
}

# pip_install_pkg_fresh <spec>
# -----------------------------------------------------------------------
# Installe/met à jour <spec> (ex: "pypostcards[ktmanager,ktimport]") en
# garantissant que le PAQUET PRINCIPAL provient toujours de PyPI (jamais
# du cache pip local), tout en laissant pip utiliser le cache local pour
# les DEPENDANCES (plus rapide, fonctionne aussi hors-ligne pour elles).
#
# Installs/upgrades <spec> (e.g. "pypostcards[ktmanager,ktimport]"),
# guaranteeing that the MAIN PACKAGE always comes from PyPI (never from
# the local pip cache), while letting pip use the local cache for the
# DEPENDENCIES (faster, also works offline for them).
#
# Doit être appelée avec le venv déjà activé (source .../bin/activate).
# Must be called with the venv already activated.
# -----------------------------------------------------------------------
pip_install_pkg_fresh() {
  local spec="$1"
  # Etape 1 : le paquet principal uniquement (--no-deps), forcé depuis
  # PyPI et en ignorant totalement le cache pip (--no-cache-dir,
  # --force-reinstall) pour être certain de ne jamais réutiliser une
  # version mise en cache localement.
  python -m pip install --no-cache-dir --force-reinstall --no-deps --upgrade "$spec"
  # Etape 2 : résolution/installation normale des dépendances. Le paquet
  # principal est déjà à la bonne version (étape 1), donc pip ne fait
  # ici qu'installer/compléter les dépendances manquantes, en utilisant
  # le cache pip local quand elles s'y trouvent déjà.
  python -m pip install "$spec"
}

# =============================================================================
# LANCEURS ET FICHIERS .desktop (fonctions réutilisées par install et update)
# =============================================================================

# ensure_data_dirs <conf_file>
# -----------------------------------------------------------------------
# Crée les répertoires datadir, importdir et tmpdir déclarés dans le
# fichier de configuration (section [DEFAULT]) s'ils n'existent pas
# encore. On relit ces valeurs directement dans le fichier de config
# (plutôt que de supposer les chemins par défaut) afin que ça fonctionne
# aussi bien pour un fichier fraîchement généré que pour un fichier déjà
# existant et éventuellement personnalisé par l'utilisateur.
#
# Creates the datadir, importdir and tmpdir directories declared in the
# configuration file ([DEFAULT] section) if they don't exist yet. These
# values are read directly from the config file (rather than assuming
# the default paths) so this works both for a freshly generated file and
# for an already existing file that the user may have customized.
# -----------------------------------------------------------------------
ensure_data_dirs() {
  local conf_file="$1"
  [[ -f "$conf_file" ]] || return 0

  local dirs
  dirs="$(python3 - "$conf_file" <<'PYEOF' 2>/dev/null || true
import configparser, sys
c = configparser.ConfigParser()
c.read(sys.argv[1])
for key in ("datadir", "importdir", "tmpdir"):
    value = c.defaults().get(key, "")
    if value:
        print(value)
PYEOF
)"

  [[ -z "$dirs" ]] && return 0

  while IFS= read -r dir; do
    [[ -z "$dir" ]] && continue
    if [[ ! -d "$dir" ]]; then
      msgln creating_data_dir "$dir"
      mkdir -p "$dir"
    fi
  done <<< "$dirs"
}

create_launchers() {
  mkdir -p "$BIN_DIR"
  msgln creating_launchers "$BIN_DIR"

  for script in "${SCRIPT_ORDER[@]}"; do
    venv_bin="$VENV_DIR/bin/$script"
    # En version "light", ktscan/kttools peuvent ne pas être disponibles
    # selon les extras réellement installés : on ne crée le lanceur que
    # si le binaire existe dans le venv.
    if [[ -x "$venv_bin" ]]; then
      launcher="$BIN_DIR/$script"
      cat > "$launcher" <<EOF
#!/usr/bin/env bash
exec "$venv_bin" --conffile "$CONF_FILE" "\$@"
EOF
      chmod +x "$launcher"
    fi
  done
}

create_desktop_files() {
  mkdir -p "$ICON_DIR"

  # On tente de localiser des icônes fournies dans le paquet installé.
  PKG_DATA_DIR="$("$VENV_DIR/bin/python" - <<'PYEOF' 2>/dev/null || true
import importlib.util, pathlib
spec = importlib.util.find_spec("tkpostcards")
if spec and spec.submodule_search_locations:
    print(list(spec.submodule_search_locations)[0])
PYEOF
)"

  FOUND_ICON=""
  if [[ -n "${PKG_DATA_DIR:-}" && -d "$PKG_DATA_DIR" ]]; then
    # Cherche une icône générique du paquet (png ou svg) dans son arborescence
    FOUND_ICON="$(find "$PKG_DATA_DIR" -type f \( -iname "*.png" -o -iname "*.svg" -o -iname "*.ico" \) 2>/dev/null | head -n1 || true)"
    if [[ -n "$FOUND_ICON" ]]; then
      cp "$FOUND_ICON" "$ICON_DIR/pypostcards$(basename "$FOUND_ICON" | sed -n 's/.*\(\.[a-zA-Z0-9]*\)$/\1/p')"
    fi
  fi

  if [[ -z "$FOUND_ICON" ]]; then
    msgln icon_not_found
    ICON_NAME="image-x-generic"
  else
    ICON_NAME="$ICON_DIR/$(ls "$ICON_DIR" | head -n1)"
  fi

  mkdir -p "$DESKTOP_DIR"
  msgln creating_desktop "$DESKTOP_DIR"

  declare -A APP_NAME_FR=([ktmanager]="$APP_NAME - Gestionnaire" [ktimport]="$APP_NAME - Import" [ktscan]="$APP_NAME - Numérisation")
  declare -A APP_NAME_EN=([ktmanager]="$APP_NAME - Manager" [ktimport]="$APP_NAME - Import" [ktscan]="$APP_NAME - Scan")
  declare -A APP_COMMENT_FR=([ktmanager]="Gérer votre collection de cartes postales" [ktimport]="Importer des cartes postales" [ktscan]="Numériser des cartes postales")
  declare -A APP_COMMENT_EN=([ktmanager]="Manage your postcard collection" [ktimport]="Import postcards" [ktscan]="Scan postcards")

  for script in "${DESKTOP_SCRIPTS[@]}"; do
    launcher="$BIN_DIR/$script"
    [[ -x "$launcher" ]] || continue

    desktop_file="$DESKTOP_DIR/kartotek-${script}.desktop"
    cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=${APP_NAME_EN[$script]}
Name[fr]=${APP_NAME_FR[$script]}
Comment=${APP_COMMENT_EN[$script]}
Comment[fr]=${APP_COMMENT_FR[$script]}
Exec=$launcher
Icon=$ICON_NAME
Terminal=false
Categories=Graphics;Office;
StartupNotify=true
EOF
    chmod +x "$desktop_file"
  done

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
  fi
}

# =============================================================================
# do_install : exécute la séquence complète d'installation
# =============================================================================

do_install() {

# =============================================================================
# ETAPE 1 : VERIFICATION DES OUTILS SYSTEME (python3, venv, pip)
# =============================================================================

msgln checking_tools

missing_tools=()

command -v python3 >/dev/null 2>&1 || missing_tools+=("python3")

if command -v python3 >/dev/null 2>&1; then
  if ! python3 -c "import venv" >/dev/null 2>&1; then
    vpkg="$(python_venv_pkg)"
    # Sur certaines distributions, venv fait partie du paquet python3 de base :
    # dans ce cas on retombe sur "python3" comme nom générique à afficher/réinstaller.
    [[ -z "$vpkg" ]] && vpkg="python3"
    missing_tools+=("$vpkg")
  fi
  python3 -m pip --version >/dev/null 2>&1 || missing_tools+=("python3-pip")
fi

if [[ "${#missing_tools[@]}" -gt 0 ]]; then
  for t in "${missing_tools[@]}"; do
    msgln tool_missing "$t"
  done
  if [[ -z "$PKG_MANAGER" ]]; then
    msgln no_pkg_manager "${missing_tools[*]}"
    exit 1
  fi
  read -rp "$(msg propose_install_tools)" _r
  if ask_yes_no <<< "$_r"; then
    pkg_install "${missing_tools[@]}"
  else
    msgln install_cancelled
    exit 1
  fi
else
  msgln tools_ok
fi

echo

# =============================================================================
# ETAPE 2 : CHOIX DE LA VERSION (LEGERE / COMPLETE)
# =============================================================================

msgln choose_variant
echo "  1) $(msg variant_light)"
echo "  2) $(msg variant_full)"
read -rp "$(msg choose_prompt)" variant_choice

case "$variant_choice" in
  2) VARIANT="full" ;;
  *) VARIANT="light" ;;
esac

PIP_EXTRA="$(pip_extra_for_variant "$VARIANT")"

echo

# =============================================================================
# ETAPE 3 (version complète uniquement) : TESSERACT
# =============================================================================

LANGS_TO_INSTALL=("${TESSERACT_LANGS_DEFAULT[@]}")

# Nom humain des langues, pour affichage
declare -A LANG_HUMAN_FR=([eng]="Anglais" [fra]="Français" [deu]="Allemand")
declare -A LANG_HUMAN_EN=([eng]="English" [fra]="French" [deu]="German")
lang_human() {
  local code="$1"
  if [[ "$LANG_CODE" == "fr" ]]; then echo "${LANG_HUMAN_FR[$code]:-$code}"; else echo "${LANG_HUMAN_EN[$code]:-$code}"; fi
}

if [[ "$VARIANT" == "full" ]]; then
  msgln checking_tesseract
  if command -v tesseract >/dev/null 2>&1; then
    tess_version="$(tesseract --version 2>&1 | head -n1)"
    msgln tesseract_found "$tess_version"
  else
    msgln tesseract_missing
    if [[ -z "$PKG_MANAGER" ]]; then
      msgln no_pkg_manager "$(tesseract_bin_pkg)"
    else
      read -rp "$(msg propose_tesseract_install)" _r
      if ask_yes_no <<< "$_r"; then
        pkg_install "$(tesseract_bin_pkg)"
      else
        msgln tesseract_skip_warning
      fi
    fi
  fi

  # Proposer les langues supplémentaires (celles pas déjà dans la liste par
  # défaut). On ne propose PAS une langue déjà réellement présente sur le
  # système (vérifiée via tesseract --list-langs) : elle est simplement
  # conservée dans LANGS_TO_INSTALL sans redemander à l'utilisateur.
  for code in "${TESSERACT_LANGS_AVAILABLE[@]}"; do
    already_default=false
    for d in "${TESSERACT_LANGS_DEFAULT[@]}"; do
      [[ "$d" == "$code" ]] && already_default=true && break
    done
    $already_default && continue

    if tesseract_lang_installed "$code"; then
      msgln lang_already_installed "$code" "$(lang_human "$code")"
      LANGS_TO_INSTALL+=("$code")
      continue
    fi

    read -rp "$(msg ask_extra_lang "$code" "$(lang_human "$code")")" _r
    if ask_yes_no <<< "$_r"; then
      LANGS_TO_INSTALL+=("$code")
    fi
  done

  # N'installe que les paquets de langues qui ne sont PAS déjà présents sur
  # le système (contrôle réel via tesseract --list-langs), pour éviter tout
  # appel inutile au gestionnaire de paquets.
  if [[ -n "$PKG_MANAGER" ]]; then
    lang_pkgs=()
    langs_missing=()
    for code in "${LANGS_TO_INSTALL[@]}"; do
      tesseract_lang_installed "$code" && continue
      lang_pkgs+=("$(tesseract_lang_pkg "$code")")
      langs_missing+=("$code")
    done
    if [[ "${#lang_pkgs[@]}" -gt 0 ]]; then
      msgln installing_lang_packs "${langs_missing[*]}"
      pkg_install "${lang_pkgs[@]}" || true
    else
      msgln no_lang_packs_to_install
    fi
  fi
  echo
fi

# =============================================================================
# ETAPE 3bis : SCANIMAGE / SANE-UTILS (utilisé par ktscan)
# =============================================================================

msgln checking_scanimage
if command -v scanimage >/dev/null 2>&1; then
  scanimage_version="$(scanimage --version 2>&1 | head -n1)"
  msgln scanimage_found "$scanimage_version"
else
  msgln scanimage_missing
  if [[ -z "$PKG_MANAGER" ]]; then
    msgln no_pkg_manager "$(sane_utils_pkg)"
  else
    read -rp "$(msg propose_scanimage_install)" _r
    if ask_yes_no <<< "$_r"; then
      pkg_install "$(sane_utils_pkg)"
    else
      msgln scanimage_skip_warning
    fi
  fi
fi

echo

# =============================================================================
# ETAPE 4 : CREATION DU VENV ET INSTALLATION DU PAQUET
# =============================================================================

mkdir -p "$(dirname "$VENV_DIR")"

if [[ -d "$VENV_DIR" ]]; then
  msgln venv_exists "$VENV_DIR"
else
  msgln creating_venv "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip >/dev/null

msgln installing_pkg "$PACKAGE_NAME" "$PIP_EXTRA"
pip_install_pkg_fresh "${PACKAGE_NAME}[${PIP_EXTRA}]"

deactivate

echo

# =============================================================================
# ETAPE 4bis : FICHIER DE CONFIGURATION (postcards.conf)
# =============================================================================

mkdir -p "$CONF_DIR"

# Mémorise la variante choisie (light/full) pour que --update sache quel
# extra réinstaller sans reposer la question.
echo "$VARIANT" > "$CONF_DIR/.variant"

if [[ -f "$CONF_FILE" ]]; then
  msgln conf_exists "$CONF_FILE"
else
  msgln creating_conf "$CONF_FILE"

  # Langues OCR au format tesseract ("fra+eng"), à partir des langues
  # définies/choisies plus haut dans ce script (LANGS_TO_INSTALL).
  OCR_LANGS="$(IFS=+; echo "${LANGS_TO_INSTALL[*]}")"

  cat > "$CONF_FILE" <<EOF
# ==============================================================================
# postcards.conf
#
# Configuration file for the "tkpostcards" application
# (tkscan, tkimport, tkmanager, tktools) and the "libpostcards" library.
#
# Format: INI (Python "configparser" module).
# Generated by install_KartoTek.sh - feel free to edit.
#
# Any key placed in [DEFAULT] is automatically inherited by all other
# sections (native configparser behavior).
# ==============================================================================


# ------------------------------------------------------------------------------
# [DEFAULT] - settings common to all tools / all sections
# ------------------------------------------------------------------------------
[DEFAULT]

# Folder where images and JSON/SQLite metadata are stored
datadir = $HOME/KartoTek/data

# Drop folder for scanned images to be imported (tkscan -> tkimport)
importdir = $HOME/KartoTek/import

# Temporary folder used by processing tasks (conversions, etc.)
tmpdir = $HOME/KartoTek/tmp

# Default file format for images (tiff, png, jpeg)
file_format = tiff

# Folder for storing session logs for remote synchronization
# (inherited by the [sync_default] section below)
logdir = $HOME/KartoTek/logs


# ------------------------------------------------------------------------------
# [tkscan] - batch scanning application (scanner)
# ------------------------------------------------------------------------------
[tkscan]

# Scanner identifier (e.g. SANE device "escl:https://192.168.1.28:443")
# Leave empty to choose the scanner from the graphical interface
scanner =

# Scan resolution in DPI
resolution = 300

# File format for saving scans (tiff, png, jpeg)
file_format = tiff

# Prefix for files generated during scanning
prefix = scanned

# Interval (in seconds) between two scans in "batch" mode
batch_interval = 30

# Interface language (2-letter ISO code: fr, en, ...). Empty = auto-detect
language =

# Enable a custom scan area (cropping at the source)
scan_area_enabled = false

# Scan area coordinates (in mm)
scan_area_left = 0
scan_area_top = 0
scan_area_width = 148
scan_area_height = 105

# Edge trimming after scan (in pixels)
crop_border = 0

# JPEG quality (0-100), used only if file_format = jpeg
jpeg_quality = 85

# PNG compression level (0-9), used only if file_format = png
png_compress = 6

# TIFF compression type (e.g. deflate, lzw, none), used if file_format = tiff
tiff_compression = deflate


# ------------------------------------------------------------------------------
# [tkimport] - review/validation application between tkscan and tkmanager
# ------------------------------------------------------------------------------
[tkimport]

# Prefix used when adding cards to the collection
prefix =

# White threshold (0-255) used to detect the transparent background
white_threshold = 240

# Interface language (2-letter ISO code). Empty = auto-detect
language =

# Tesseract languages
ocr_langs = $OCR_LANGS

# Remove files from the import folder once added to the collection
remove_after_add = false

# Preferred external application for editing an image, per platform
# (leave empty to use the system's default application)
editor_linux =
editor_macos =
editor_windows =


# ------------------------------------------------------------------------------
# [tkmanager] - collection management / browsing application
# ------------------------------------------------------------------------------
[tkmanager]

# List of known collections, comma-separated
collections = collection1, collection2

# Last collection filter used (managed automatically by the app)
last_filter =

# Last card ID displayed (managed automatically by the app)
last_id =

# Publish the full data set (routes, ...) by default during publish
publish_full = 0

# Default similarity threshold (%) for similar card search
search_threshold = 70

# Default maximum number of results for similarity search
search_max_results = 20

# Default similarity threshold (%) for duplicate detection
doubles_threshold = 90


# ------------------------------------------------------------------------------
# [sync_default] - remote synchronization section (used by
# "tktools publish"). The name of this section is free: it corresponds to
# the "config" argument passed to the "publish" command (e.g. tktools publish
# sync_default). Several sections of this type can be defined to publish
# to different destinations (e.g. [sync_prod], [sync_staging], ...).
# ------------------------------------------------------------------------------
[sync_default]

# Transfer protocol: ftp, ftps, ftptls or sftp
protocol = sftp

# Remote server address
host = ftp.example.com

# Connection port (default: 21=ftp/ftptls, 990=ftps, 22=sftp)
port = 22

# Login credentials
username = my_username
password = my_password

# Path to an SSH private key (used only for SFTP, replaces the password)
ssh_key_path =

# Base remote folder where files will be uploaded
remote_base_dir = /postcards

# Passive mode for FTP/FTPS/FTPTLS connections
passive_mode = true

# Network timeout (in seconds)
timeout = 30

# Delete files on the remote side that no longer exist locally
delete_orphans = false

# Simulate synchronization without actually transferring files
dry_run = false

# Number of simultaneous transfers when syncing a folder
max_workers = 5

# Suffix of the lock file used for remote locking (fetch_locked)
lock_suffix = .lck

# Interval (in seconds) between two checks of the remote lock
lock_poll_interval = 2.0

# Delay (in seconds) before giving up if the remote lock stays in place too long
lock_timeout = 60.0
EOF
fi

# S'assure que datadir/importdir/tmpdir existent, que le fichier de conf
# vienne d'être créé ou qu'il existait déjà (chemins potentiellement
# personnalisés par l'utilisateur).
ensure_data_dirs "$CONF_FILE"

echo

# =============================================================================
# ETAPE 5 : LANCEURS DANS ~/.local/bin
# =============================================================================

create_launchers

# =============================================================================
# ETAPE 6 : ICONES ET FICHIERS .desktop
# =============================================================================

create_desktop_files

# =============================================================================
# RESUME FINAL
# =============================================================================

echo
msgln done
if [[ "$VARIANT" == "light" ]]; then
  msgln summary_light
else
  msgln summary_full
  msgln summary_langs "${LANGS_TO_INSTALL[*]}"
fi
msgln summary_launch "${SCRIPT_ORDER[*]}"
msgln summary_conf "$CONF_FILE"

case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *)
    echo
    msgln path_reminder "$BIN_DIR"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac
}

# =============================================================================
# do_update : met à jour le paquet PyPI dans le venv existant, régénère les
# lanceurs et les .desktop. Ne redemande pas la variante (elle est mémorisée
# dans $CONF_DIR/.variant lors de l'installation) et ne touche pas au fichier
# de configuration existant.
# =============================================================================

do_update() {
  echo "$APP_NAME updater / Mise à jour $APP_NAME"
  msgln update_title "$APP_NAME"
  echo

  if [[ ! -d "$VENV_DIR" ]]; then
    msgln update_no_install "$VENV_DIR" "$(basename "$0")"
    exit 1
  fi

  VARIANT="full"
  if [[ -f "$CONF_DIR/.variant" ]]; then
    VARIANT="$(cat "$CONF_DIR/.variant")"
  fi
  PIP_EXTRA="$(pip_extra_for_variant "$VARIANT")"

  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
  python -m pip install --upgrade pip >/dev/null
  msgln updating_pkg "$PACKAGE_NAME" "$PIP_EXTRA"
  pip_install_pkg_fresh "${PACKAGE_NAME}[${PIP_EXTRA}]"

  # Mode "complet" : met aussi à jour TOUS les autres paquets déjà présents
  # dans le venv (au-delà de $PACKAGE_NAME et de ses dépendances). Utilise
  # le comportement pip normal (cache local autorisé) pour ces paquets.
  if [[ "$UPDATE_MODE" == "complete" ]]; then
    echo
    msgln updating_all_pkgs "$PACKAGE_NAME"
    outdated_pkgs="$(python -m pip list --outdated --format=freeze 2>/dev/null | cut -d'=' -f1)"
    if [[ -n "$outdated_pkgs" ]]; then
      while IFS= read -r outdated_pkg; do
        [[ -z "$outdated_pkg" ]] && continue
        python -m pip install --upgrade "$outdated_pkg"
      done <<< "$outdated_pkgs"
      msgln update_all_done
    else
      msgln no_outdated_pkgs
    fi
  fi

  deactivate

  echo

  # Régénère les lanceurs et les .desktop (au cas où la nouvelle version
  # ajoute/retire des scripts), sans toucher au fichier de configuration.
  create_launchers
  create_desktop_files

  # Recrée datadir/importdir/tmpdir s'ils ont disparu depuis l'installation.
  ensure_data_dirs "$CONF_FILE"

  echo
  msgln update_done
  msgln summary_conf "$CONF_FILE"

  case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;
    *)
      echo
      msgln path_reminder "$BIN_DIR"
      echo "  export PATH=\"$BIN_DIR:\$PATH\""
      ;;
  esac
}

# =============================================================================
# do_uninstall : supprime le venv (paquets Python installés via pip),
# les lanceurs, les fichiers .desktop et les icônes.
# NE désinstalle PAS les paquets système (tesseract, sane-utils,
# python3-venv, ...) installés via le gestionnaire de paquets : ceux-ci
# restent en place et doivent être retirés manuellement si besoin.
# Le fichier de configuration (postcards.conf) et les données de
# l'utilisateur (cartes, images) ne sont jamais supprimés sans confirmation
# explicite ; les données elles-mêmes ne sont jamais supprimées par ce script.
# =============================================================================

do_uninstall() {
  echo "$APP_NAME uninstaller / Désinstallation $APP_NAME"
  msgln uninstall_title "$APP_NAME"
  echo

  local found_something=false
  [[ -d "$VENV_DIR" ]] && found_something=true
  [[ -d "$BIN_DIR" ]] && for s in "${SCRIPT_ORDER[@]}"; do [[ -e "$BIN_DIR/$s" ]] && found_something=true; done

  if ! $found_something; then
    msgln uninstall_no_install
    exit 0
  fi

  if [[ -d "$BIN_DIR" ]]; then
    msgln uninstall_removing_launchers "$BIN_DIR"
    for script in "${SCRIPT_ORDER[@]}"; do
      rm -f "$BIN_DIR/$script"
    done
  fi

  if [[ -d "$DESKTOP_DIR" ]]; then
    msgln uninstall_removing_desktop "$DESKTOP_DIR"
    for script in "${DESKTOP_SCRIPTS[@]}"; do
      rm -f "$DESKTOP_DIR/kartotek-${script}.desktop"
    done
    if command -v update-desktop-database >/dev/null 2>&1; then
      update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
    fi
  fi

  if [[ -d "$ICON_DIR" ]]; then
    msgln uninstall_removing_icons "$ICON_DIR"
    rm -rf "$ICON_DIR"
  fi

  if [[ -d "$VENV_DIR" ]]; then
    msgln uninstall_removing_venv "$VENV_DIR"
    rm -rf "$VENV_DIR"
  fi
  rm -f "$CONF_DIR/.variant"

  echo
  msgln uninstall_keep_pkg_note

  # Le fichier de configuration n'est supprimé que sur confirmation explicite :
  # il contient les chemins vers les données de l'utilisateur, pas les
  # données elles-mêmes.
  if [[ -f "$CONF_FILE" ]]; then
    read -rp "$(msg ask_remove_conf "$CONF_FILE")" _r
    if ask_yes_no <<< "$_r"; then
      rm -f "$CONF_FILE"
      msgln conf_removed "$CONF_FILE"
    else
      msgln conf_kept "$CONF_FILE"
    fi
  fi

  echo
  msgln data_dirs_kept_note "$HOME/KartoTek"
  msgln uninstall_done
}

# =============================================================================
# DISPATCH
# =============================================================================

case "$ACTION" in
  install)   do_install ;;
  update)    do_update ;;
  uninstall) do_uninstall ;;
esac
