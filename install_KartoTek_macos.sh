#!/usr/bin/env bash
#
# install_KartoTek_macos.sh
# ---------------------------------------------------------------------------
# Installe PyPostcards (KartoTek) depuis PyPI dans un environnement virtuel
# Python, sous macOS.
# Installs PyPostcards (KartoTek) from PyPI inside a Python virtual
# environment, on macOS.
#
# Usage: ./install_KartoTek_macos.sh
# ---------------------------------------------------------------------------

set -euo pipefail

# =============================================================================
# VERSION DU SCRIPT
# -----------------------------------------------------------------------------
# IMPORTANT : incrémentez le numéro MINEUR (X.Y.Z -> X.Y+1.Z) à CHAQUE
# modification de ce script, aussi petite soit-elle. Le numéro MAJEUR est
# réservé aux changements de comportement importants ou aux ruptures de
# compatibilité. Gardez ce numéro aligné avec install_KartoTek.sh /
# install_KartoTek.ps1 dans la mesure du possible.
# =============================================================================
SCRIPT_VERSION="1.0.0"

# =============================================================================
# CONFIGURATION (modifiable facilement / easy to edit)
# =============================================================================

PACKAGE_NAME="pypostcards"
APP_NAME="KartoTek"

# Emplacement du venv (surchargeable via variable d'environnement)
VENV_DIR="${PYPOSTCARDS_VENV_DIR:-$HOME/Library/Application Support/PyPostcards/venv}"

# Emplacement du fichier de configuration
CONF_DIR="$HOME/Library/Application Support/PyPostcards"
CONF_FILE="$CONF_DIR/postcards.conf"

# Où poser les lanceurs et les .app
BIN_DIR="$HOME/.local/bin"
ICON_DIR="$CONF_DIR/icons"
APPS_DIR="$HOME/Applications/$APP_NAME"

# Scripts / entry points fournis par le paquet
declare -A ENTRY_POINTS=(
  [kttools]="tkpostcards.scripts.tktools:cli"
  [ktmanager]="tkpostcards.tkmanager:run"
  [ktimport]="tkpostcards.tkimport:run"
  [ktscan]="tkpostcards.tkscan:run"
)
SCRIPT_ORDER=(kttools ktmanager ktimport ktscan)

# Scripts pour lesquels on crée un .app (GUI). kttools est un CLI.
DESKTOP_SCRIPTS=(ktmanager ktimport ktscan)

# --- Langues Tesseract ------------------------------------------------------
# Langues installées PAR DEFAUT (codes tesseract : fra, eng, deu, ...)
TESSERACT_LANGS_DEFAULT=("fra" "eng")

# =============================================================================
# INTERNATIONALISATION / I18N
# =============================================================================

detect_lang() {
  local probe
  for probe in "${LANGUAGE:-}" "${LC_ALL:-}" "${LANG:-}"; do
    [[ -z "$probe" ]] && continue
    probe="${probe%%:*}"
    case "$probe" in
      fr*|FR*) echo "fr"; return ;;
    esac
  done
  echo "en"
}
LANG_CODE="$(detect_lang)"

declare -A MSG_FR=(
  [title]="=== Installation de %s (paquet PyPI : %s) ==="
  [checking_tools]="Vérification des outils système requis (python3, venv, pip)..."
  [tool_missing]="Outil manquant : %s"
  [propose_install_tools]="Certains outils système sont manquants. Les installer maintenant avec Homebrew ? [o/N] "
  [tools_ok]="Tous les outils système nécessaires sont présents."
  [no_brew]="Homebrew n'est pas installé. Installez-le d'abord depuis https://brew.sh puis relancez ce script. Outils requis : %s"
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
  [propose_tesseract_install]="Installer tesseract maintenant avec Homebrew ? [o/N] "
  [tesseract_skip_warning]="ATTENTION : sans tesseract, les fonctionnalités OCR ne fonctionneront pas."
  [checking_tess_langs]="Vérification des langues Tesseract installées (%s)..."
  [tess_langs_ok]="Les langues Tesseract souhaitées sont déjà installées."
  [propose_tess_lang_install]="Certaines langues Tesseract (%s) sont manquantes. Homebrew ne permet pas de choisir une langue à la fois : la formule 'tesseract-lang' installe TOUTES les langues supplémentaires en une fois (téléchargement volumineux). L'installer maintenant ? [o/N] "
  [tess_lang_skip_warning]="ATTENTION : sans ces données de langue, l'OCR pour certaines langues ne fonctionnera pas."
  [checking_scanimage]="Vérification de la présence de scanimage (sane-backends)..."
  [scanimage_found]="scanimage est déjà installé (%s)."
  [scanimage_missing]="scanimage n'est pas installé mais est requis par ktscan."
  [propose_scanimage_install]="Installer sane-backends (scanimage) maintenant avec Homebrew ? [o/N] "
  [scanimage_skip_warning]="ATTENTION : sans scanimage/sane-backends, ktscan ne pourra pas numériser. Note : selon votre scanner, l'application Transfert d'images (Image Capture) d'Apple peut aussi fonctionner."
  [creating_conf]="Création du fichier de configuration dans %s ..."
  [conf_exists]="Un fichier de configuration existe déjà dans %s, il est conservé tel quel."
  [creating_data_dir]="Création du dossier manquant : %s"
  [summary_conf]="Fichier de configuration : %s"
  [creating_launchers]="Création des lanceurs dans %s ..."
  [creating_apps]="Création des applications (.app) dans %s ..."
  [icon_not_found]="Aucune icône trouvée dans le paquet, l'icône par défaut sera utilisée."
  [path_reminder]="Remarque : ajoutez %s à votre PATH si ce n'est pas déjà fait (ex. dans ~/.zshrc) :"
  [done]="Installation terminée !"
  [summary_light]="Version installée : légère (ktmanager, ktimport)"
  [summary_full]="Version installée : complète (ktmanager, ktimport, ocr, similar, travel)"
  [summary_langs]="Langues Tesseract souhaitées : %s"
  [summary_launch]="Vous pouvez lancer les outils avec : %s"
  [summary_apps]="Les applications sont aussi disponibles dans %s (visibles depuis Launchpad/Spotlight)."
  [yes_no_hint]="(o = oui / n = non)"
  [version_line]="%s - version du script : %s"
  [usage]="Usage : %s [--install|--update|--update-complete|--uninstall|--version|--help]"
  [help_install]="  --install               Installe (ou réinstalle) %s (option par défaut)"
  [help_update]="  --update, --upgrade     Met à jour %s vers la dernière version depuis PyPI"
  [help_update_complete]="  --update-complete, --update-all"
  [help_update_complete_note]="                          Comme --update, et met aussi à jour TOUS les paquets du venv"
  [help_uninstall]="  --uninstall             Désinstalle %s (venv, lanceurs, .app, icônes)"
  [help_uninstall_note]="                          Ne supprime PAS les paquets système (tesseract, sane-backends, ...)"
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
  [uninstall_removing_apps]="Suppression des applications dans %s"
  [uninstall_removing_icons]="Suppression des icônes dans %s"
  [uninstall_keep_pkg_note]="Note : les paquets système (tesseract, sane-backends, python, ...) installés via Homebrew ne sont PAS supprimés. Désinstallez-les manuellement (brew uninstall ...) si besoin."
  [ask_remove_conf]="Supprimer aussi le fichier de configuration (%s) ? [o/N] "
  [conf_removed]="Fichier de configuration supprimé : %s"
  [conf_kept]="Fichier de configuration conservé : %s"
  [data_dirs_kept_note]="Remarque : vos données (cartes, images, dans %s) n'ont PAS été supprimées."
  [uninstall_done]="Désinstallation terminée."
  [gatekeeper_note]="Remarque macOS : si le Finder refuse d'ouvrir une application créée par ce script (« développeur non identifié »), faites un clic droit (ou Ctrl-clic) sur l'application puis choisissez « Ouvrir » une première fois."
)

declare -A MSG_EN=(
  [title]="=== %s installation (PyPI package: %s) ==="
  [checking_tools]="Checking required system tools (python3, venv, pip)..."
  [tool_missing]="Missing tool: %s"
  [propose_install_tools]="Some system tools are missing. Install them now with Homebrew? [y/N] "
  [tools_ok]="All required system tools are present."
  [no_brew]="Homebrew is not installed. Install it first from https://brew.sh then re-run this script. Required tools: %s"
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
  [propose_tesseract_install]="Install tesseract now with Homebrew? [y/N] "
  [tesseract_skip_warning]="WARNING: without tesseract, OCR features will not work."
  [checking_tess_langs]="Checking installed Tesseract languages (%s)..."
  [tess_langs_ok]="The requested Tesseract languages are already installed."
  [propose_tess_lang_install]="Some Tesseract languages (%s) are missing. Homebrew cannot install a single language at a time: the 'tesseract-lang' formula installs ALL extra languages at once (large download). Install it now? [y/N] "
  [tess_lang_skip_warning]="WARNING: without this language data, OCR for some languages will not work."
  [checking_scanimage]="Checking for scanimage (sane-backends)..."
  [scanimage_found]="scanimage is already installed (%s)."
  [scanimage_missing]="scanimage is not installed but is required by ktscan."
  [propose_scanimage_install]="Install sane-backends (scanimage) now with Homebrew? [y/N] "
  [scanimage_skip_warning]="WARNING: without scanimage/sane-backends, ktscan will not be able to scan. Note: depending on your scanner, Apple's Image Capture app may also work."
  [creating_conf]="Creating configuration file in %s ..."
  [conf_exists]="A configuration file already exists in %s, it is kept as is."
  [creating_data_dir]="Creating missing folder: %s"
  [summary_conf]="Configuration file: %s"
  [creating_launchers]="Creating launchers in %s ..."
  [creating_apps]="Creating applications (.app) in %s ..."
  [icon_not_found]="No icon found in the package, the default icon will be used."
  [path_reminder]="Note: add %s to your PATH if it isn't already (e.g. in ~/.zshrc):"
  [done]="Installation complete!"
  [summary_light]="Installed version: light (ktmanager, ktimport)"
  [summary_full]="Installed version: full (ktmanager, ktimport, ocr, similar, travel)"
  [summary_langs]="Requested Tesseract languages: %s"
  [summary_launch]="You can launch the tools with: %s"
  [summary_apps]="The applications are also available in %s (visible from Launchpad/Spotlight)."
  [yes_no_hint]="(y = yes / n = no)"
  [version_line]="%s - script version: %s"
  [usage]="Usage: %s [--install|--update|--update-complete|--uninstall|--version|--help]"
  [help_install]="  --install               Install (or reinstall) %s (default)"
  [help_update]="  --update, --upgrade     Update %s to the latest version from PyPI"
  [help_update_complete]="  --update-complete, --update-all"
  [help_update_complete_note]="                          Same as --update, and also update ALL packages in the venv"
  [help_uninstall]="  --uninstall             Uninstall %s (venv, launchers, .app, icons)"
  [help_uninstall_note]="                          Does NOT remove system packages (tesseract, sane-backends, ...)"
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
  [uninstall_removing_apps]="Removing applications from %s"
  [uninstall_removing_icons]="Removing icons from %s"
  [uninstall_keep_pkg_note]="Note: system packages (tesseract, sane-backends, python, ...) installed via Homebrew are NOT removed. Uninstall them manually (brew uninstall ...) if needed."
  [ask_remove_conf]="Also remove the configuration file (%s)? [y/N] "
  [conf_removed]="Configuration file removed: %s"
  [conf_kept]="Configuration file kept: %s"
  [data_dirs_kept_note]="Note: your data (cards, images, in %s) was NOT removed."
  [uninstall_done]="Uninstall complete."
  [gatekeeper_note]="macOS note: if Finder refuses to open an application created by this script (\"unidentified developer\"), right-click (or Ctrl-click) the app and choose \"Open\" once."
)

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
  local reply
  read -r reply || true
  case "$reply" in
    [oOyY]*) return 0 ;;
    *) return 1 ;;
  esac
}

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
UPDATE_MODE="normal"
for arg in "$@"; do
  case "$arg" in
    --install) ACTION="install" ;;
    --update|--upgrade) ACTION="update"; UPDATE_MODE="normal" ;;
    --update-complete|--update-all|--upgrade-complete|--upgrade-all) ACTION="update"; UPDATE_MODE="complete" ;;
    --uninstall) ACTION="uninstall" ;;
    --version|-v) msgln version_line "$APP_NAME" "$SCRIPT_VERSION"; exit 0 ;;
    --help|-h) print_usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; print_usage; exit 1 ;;
  esac
done

if [[ "$ACTION" == "install" ]]; then
  echo "$APP_NAME installer / Installateur $APP_NAME"
  msgln title "$APP_NAME" "$PACKAGE_NAME"
  msgln version_line "$APP_NAME" "$SCRIPT_VERSION"
  echo
fi

# =============================================================================
# DETECTION DE HOMEBREW
# =============================================================================

HAS_BREW=false
command -v brew >/dev/null 2>&1 && HAS_BREW=true

BREW_UPDATED=false
brew_update_once() {
  $BREW_UPDATED && return 0
  brew update >/dev/null 2>&1 || true
  BREW_UPDATED=true
}

brew_install() {
  # brew_install formula1 formula2 ...
  brew_update_once
  brew install "$@"
}

python_pkg_name() { echo "python@3.12"; }
tesseract_pkg_name() { echo "tesseract"; }
tesseract_lang_pkg_name() { echo "tesseract-lang"; }
sane_pkg_name() { echo "sane-backends"; }

# tesseract_lang_installed <code>
tesseract_lang_installed() {
  local code="$1"
  command -v tesseract >/dev/null 2>&1 || return 1
  tesseract --list-langs 2>/dev/null | grep -qx "$code"
}

# Extras pip selon la variante choisie
pip_extra_for_variant() {
  case "$1" in
    light) echo "ktmanager,ktimport" ;;
    full)  echo "ktmanager,ktimport,ocr,similar,travel" ;;
    *)     echo "ktmanager,ktimport" ;;
  esac
}

# pip_install_pkg_fresh <spec>
# Le paquet principal vient toujours de PyPI (jamais du cache local), les
# dépendances peuvent utiliser le cache pip normal.
pip_install_pkg_fresh() {
  local spec="$1"
  python -m pip install --no-cache-dir --force-reinstall --no-deps --upgrade "$spec"
  python -m pip install "$spec"
}

# =============================================================================
# LANCEURS ET APPLICATIONS .app (fonctions réutilisées par install et update)
# =============================================================================

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

# find_pkg_icon : cherche un png/svg fourni par le paquet installé
find_pkg_icon() {
  local pkg_data_dir
  pkg_data_dir="$("$VENV_DIR/bin/python" - <<'PYEOF' 2>/dev/null || true
import importlib.util
spec = importlib.util.find_spec("tkpostcards")
if spec and spec.submodule_search_locations:
    print(list(spec.submodule_search_locations)[0])
PYEOF
)"
  [[ -n "$pkg_data_dir" && -d "$pkg_data_dir" ]] || return 0
  find "$pkg_data_dir" -type f \( -iname "*.png" -o -iname "*.icns" \) 2>/dev/null | head -n1 || true
}

# make_icns <source_png> <output_icns>
# Construit un .icns basique à partir d'un PNG via sips/iconutil (outils
# Apple standards). Best-effort : en cas d'échec, l'app utilisera l'icône
# générique du Finder.
make_icns() {
  local src="$1" out="$2"
  command -v sips >/dev/null 2>&1 || return 1
  command -v iconutil >/dev/null 2>&1 || return 1
  local tmp_iconset
  tmp_iconset="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$tmp_iconset"
  local size
  for size in 16 32 64 128 256 512; do
    sips -z "$size" "$size" "$src" --out "$tmp_iconset/icon_${size}x${size}.png" >/dev/null 2>&1 || return 1
    local size2=$((size * 2))
    sips -z "$size2" "$size2" "$src" --out "$tmp_iconset/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
  done
  iconutil -c icns "$tmp_iconset" -o "$out" >/dev/null 2>&1 || return 1
  rm -rf "$(dirname "$tmp_iconset")"
  return 0
}

# create_app_bundle <script_name> <display_name>
# Crée une .app minimale (Contents/MacOS + Info.plist) qui exécute le
# lanceur correspondant dans $BIN_DIR. Permet de retrouver l'outil depuis
# Launchpad / Spotlight, sans passer par le terminal.
create_app_bundle() {
  local script="$1" display_name="$2"
  local launcher="$BIN_DIR/$script"
  [[ -x "$launcher" ]] || return 0

  local app_dir="$APPS_DIR/${display_name}.app"
  local contents="$app_dir/Contents"
  local macos_dir="$contents/MacOS"
  mkdir -p "$macos_dir" "$contents/Resources"

  cat > "$macos_dir/$script" <<EOF
#!/usr/bin/env bash
exec "$launcher" "\$@"
EOF
  chmod +x "$macos_dir/$script"

  local icon_key=""
  if [[ -f "$ICON_DIR/AppIcon.icns" ]]; then
    cp "$ICON_DIR/AppIcon.icns" "$contents/Resources/AppIcon.icns"
    icon_key="  <key>CFBundleIconFile</key>
  <string>AppIcon</string>"
  fi

  cat > "$contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>${display_name}</string>
  <key>CFBundleDisplayName</key>
  <string>${display_name}</string>
  <key>CFBundleIdentifier</key>
  <string>com.kartotek.${script}</string>
  <key>CFBundleVersion</key>
  <string>${SCRIPT_VERSION}</string>
  <key>CFBundleExecutable</key>
  <string>${script}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
${icon_key}
</dict>
</plist>
EOF
}

create_apps() {
  mkdir -p "$ICON_DIR" "$APPS_DIR"
  msgln creating_apps "$APPS_DIR"

  local found_icon
  found_icon="$(find_pkg_icon)"
  if [[ -n "$found_icon" ]]; then
    if [[ "$found_icon" == *.icns ]]; then
      cp "$found_icon" "$ICON_DIR/AppIcon.icns"
    else
      make_icns "$found_icon" "$ICON_DIR/AppIcon.icns" || true
    fi
  fi
  [[ -f "$ICON_DIR/AppIcon.icns" ]] || msgln icon_not_found

  declare -A APP_TITLE_FR=([ktmanager]="$APP_NAME - Gestionnaire" [ktimport]="$APP_NAME - Import" [ktscan]="$APP_NAME - Numérisation")
  declare -A APP_TITLE_EN=([ktmanager]="$APP_NAME - Manager" [ktimport]="$APP_NAME - Import" [ktscan]="$APP_NAME - Scan")

  for script in "${DESKTOP_SCRIPTS[@]}"; do
    local title
    if [[ "$LANG_CODE" == "fr" ]]; then title="${APP_TITLE_FR[$script]}"; else title="${APP_TITLE_EN[$script]}"; fi
    create_app_bundle "$script" "$title"
  done
}

# =============================================================================
# do_install : exécute la séquence complète d'installation
# =============================================================================

do_install() {

# --- ETAPE 1 : outils système requis (Homebrew, python3, venv, pip) --------
msgln checking_tools

missing_tools=()
command -v python3 >/dev/null 2>&1 || missing_tools+=("python3")
if command -v python3 >/dev/null 2>&1; then
  python3 -c "import venv" >/dev/null 2>&1 || missing_tools+=("$(python_pkg_name)")
  python3 -m pip --version >/dev/null 2>&1 || missing_tools+=("$(python_pkg_name)")
fi
# dédoublonnage simple
missing_tools=($(printf "%s\n" "${missing_tools[@]}" | sort -u))

if [[ "${#missing_tools[@]}" -gt 0 ]]; then
  for t in "${missing_tools[@]}"; do
    msgln tool_missing "$t"
  done
  if ! $HAS_BREW; then
    msgln no_brew "${missing_tools[*]}"
    exit 1
  fi
  read -rp "$(msg propose_install_tools)" _r
  if ask_yes_no <<< "$_r"; then
    brew_install "${missing_tools[@]}"
  else
    msgln install_cancelled
    exit 1
  fi
else
  msgln tools_ok
fi

echo

# --- ETAPE 2 : choix de la version (légère / complète) ---------------------
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

# --- ETAPE 3 (version complète) : Tesseract ---------------------------------
LANGS_TO_INSTALL=("${TESSERACT_LANGS_DEFAULT[@]}")

if [[ "$VARIANT" == "full" ]]; then
  msgln checking_tesseract
  if command -v tesseract >/dev/null 2>&1; then
    tess_version="$(tesseract --version 2>&1 | head -n1)"
    msgln tesseract_found "$tess_version"
  else
    msgln tesseract_missing
    if ! $HAS_BREW; then
      msgln no_brew "$(tesseract_pkg_name)"
    else
      read -rp "$(msg propose_tesseract_install)" _r
      if ask_yes_no <<< "$_r"; then
        brew_install "$(tesseract_pkg_name)"
      else
        msgln tesseract_skip_warning
      fi
    fi
  fi

  msgln checking_tess_langs "${LANGS_TO_INSTALL[*]}"
  missing_langs=()
  for code in "${LANGS_TO_INSTALL[@]}"; do
    tesseract_lang_installed "$code" || missing_langs+=("$code")
  done

  if [[ "${#missing_langs[@]}" -eq 0 ]]; then
    msgln tess_langs_ok
  elif $HAS_BREW; then
    read -rp "$(msg propose_tess_lang_install "${missing_langs[*]}")" _r
    if ask_yes_no <<< "$_r"; then
      brew_install "$(tesseract_lang_pkg_name)"
    else
      msgln tess_lang_skip_warning
    fi
  else
    msgln no_brew "$(tesseract_lang_pkg_name)"
  fi
  echo
fi

# --- ETAPE 3bis : SCANIMAGE / SANE-BACKENDS (utilisé par ktscan) -----------
msgln checking_scanimage
if command -v scanimage >/dev/null 2>&1; then
  scanimage_version="$(scanimage --version 2>&1 | head -n1)"
  msgln scanimage_found "$scanimage_version"
else
  msgln scanimage_missing
  if ! $HAS_BREW; then
    msgln no_brew "$(sane_pkg_name)"
  else
    read -rp "$(msg propose_scanimage_install)" _r
    if ask_yes_no <<< "$_r"; then
      brew_install "$(sane_pkg_name)"
    else
      msgln scanimage_skip_warning
    fi
  fi
fi

echo

# --- ETAPE 4 : création du venv et installation du paquet ------------------
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

# --- ETAPE 4bis : fichier de configuration (postcards.conf) ---------------
mkdir -p "$CONF_DIR"
echo "$VARIANT" > "$CONF_DIR/.variant"

if [[ -f "$CONF_FILE" ]]; then
  msgln conf_exists "$CONF_FILE"
else
  msgln creating_conf "$CONF_FILE"
  OCR_LANGS="$(IFS=+; echo "${LANGS_TO_INSTALL[*]}")"

  cat > "$CONF_FILE" <<EOF
# ==============================================================================
# postcards.conf
#
# Configuration file for the "tkpostcards" application
# (tkscan, tkimport, tkmanager, tktools) and the "libpostcards" library.
#
# Format: INI (Python "configparser" module).
# Generated by install_KartoTek_macos.sh - feel free to edit.
#
# Any key placed in [DEFAULT] is automatically inherited by all other
# sections (native configparser behavior).
# ==============================================================================

[DEFAULT]
datadir = $HOME/KartoTek/data
importdir = $HOME/KartoTek/import
tmpdir = $HOME/KartoTek/tmp
file_format = tiff
logdir = $HOME/KartoTek/logs

[tkscan]
scanner =
resolution = 300
file_format = tiff
prefix = scanned
batch_interval = 30
language =
scan_area_enabled = false
scan_area_left = 0
scan_area_top = 0
scan_area_width = 148
scan_area_height = 105
crop_border = 0
jpeg_quality = 85
png_compress = 6
tiff_compression = deflate

[tkimport]
prefix =
white_threshold = 240
language =
ocr_langs = $OCR_LANGS
remove_after_add = false
editor_linux =
editor_macos =
editor_windows =

[tkmanager]
collections = collection1, collection2
last_filter =
last_id =
publish_full = 0
search_threshold = 70
search_max_results = 20
doubles_threshold = 90

[sync_default]
protocol = sftp
host = ftp.example.com
port = 22
username = my_username
password = my_password
ssh_key_path =
remote_base_dir = /postcards
passive_mode = true
timeout = 30
delete_orphans = false
dry_run = false
max_workers = 5
lock_suffix = .lck
lock_poll_interval = 2.0
lock_timeout = 60.0
EOF
fi

ensure_data_dirs "$CONF_FILE"

echo

# --- ETAPE 5 : lanceurs dans ~/.local/bin -----------------------------------
create_launchers

# --- ETAPE 6 : applications .app (Launchpad/Spotlight) ----------------------
create_apps

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
msgln summary_apps "$APPS_DIR"
msgln summary_conf "$CONF_FILE"

case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *)
    echo
    msgln path_reminder "$BIN_DIR"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

echo
msgln gatekeeper_note
}

# =============================================================================
# do_update : met à jour le paquet PyPI dans le venv existant, régénère les
# lanceurs et les .app. Ne redemande pas la variante (mémorisée dans
# $CONF_DIR/.variant) et ne touche pas au fichier de configuration existant.
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

  create_launchers
  create_apps

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
# do_uninstall : supprime le venv, les lanceurs, les .app et les icônes.
# NE désinstalle PAS les paquets Homebrew (tesseract, sane-backends, ...) :
# ceux-ci restent en place et doivent être retirés manuellement si besoin.
# Le fichier de configuration et les données utilisateur ne sont jamais
# supprimés sans confirmation explicite.
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

  if [[ -d "$APPS_DIR" ]]; then
    msgln uninstall_removing_apps "$APPS_DIR"
    rm -rf "$APPS_DIR"
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
