#!/usr/bin/env bash
#
# build-appimage.sh - Construit une (ou plusieurs) AppImage multi-entrees
#                      pour pypostcards (kttools, ktmanager, ktimport, ktscan)
# --------------------------------------------------------------------
#
# Chaque AppImage produite (dist/<APP_ID>[-<variante>]-<arch>.AppImage)
# embarque les 4 points d'entree du projet :
#   kttools   -> tkpostcards.scripts.tktools:cli   (CLI, groupe click)
#   ktmanager -> tkpostcards.tkmanager:run          (GUI tkinter, defaut)
#   ktimport  -> tkpostcards.tkimport:run           (GUI tkinter)
#   ktscan    -> tkpostcards.tkscan:run             (GUI tkinter)
#
# Selection de l'outil au lancement (dans cet ordre de priorite) :
#   1. Nom utilise pour invoquer l'AppImage (symlink). Le script cree
#      automatiquement, a cote de chaque AppImage, 4 symlinks kttools /
#      ktmanager / ktimport / ktscan pointant dessus.
#   2. Premier argument, s'il correspond a l'un des 4 noms :
#         ./pypostcards-x86_64.AppImage ktscan --conffile mon.conf
#   3. A defaut : ktmanager (interface principale).
#
# Variantes GPU (--gpu)
# -----------------------
# La fonctionnalite "similar" (recherche de cartes similaires/doublons,
# commande "kttools similar") s'appuie sur torch/torchvision/open-clip-
# torch. Ces paquets existent en plusieurs variantes selon l'accelerateur
# materiel cible ; --gpu permet de choisir laquelle embarquer (une ou
# plusieurs AppImages distinctes sont alors produites) :
#   --gpu=cpu     (defaut) torch CPU-only, tourne partout, la plus legere.
#   --gpu=nvidia  torch avec support CUDA (GPU NVIDIA). Necessite un
#                 pilote NVIDIA compatible avec CUDA_VERSION sur la
#                 machine qui EXECUTE l'AppImage.
#   --gpu=amd     torch avec support ROCm (GPU AMD). Necessite ROCm/le
#                 pilote adequat sur la machine qui EXECUTE l'AppImage.
# Plusieurs variantes a la fois : --gpu=cpu,nvidia,amd (une AppImage par
# variante, suffixee d'apres la version reelle embarquee, ex:
# -cpu / -cu128 / -rocm7.2, dans dist/).
#   CUDA_VERSION=cu128 (defaut, modifiable : cu118, cu126, cu128, cu130, ...)
#   ROCM_VERSION=rocm7.2 (defaut, modifiable selon https://pytorch.org/get-started/locally/)
# Ces variantes sont volumineuses (plusieurs centaines de Mo a quelques
# Go) : elles necessitent un acces reseau a download.pytorch.org et
# suffisamment d'espace disque sur la machine de build.
# --no-similar desactive entierement cette fonctionnalite (ni torch, ni
# open-clip-torch, ni imagehash -- ignore alors --gpu).
#
# Fonctionnalite "travel" (--no-travel)
# ---------------------------------------
# Embarque ortools (Google OR-Tools, optimisation d'itineraire). Paquet
# leger (pas de variante GPU). Actif par defaut, --no-travel pour
# l'omettre.
#
# Dependances binaires embarquees
# --------------------------------
#   - tesseract-ocr (binaire + bibliotheques privees + donnees de langue)
#     requis par pytesseract. Il n'existe pas de paquet pip pour ca, on
#     le recopie donc depuis la machine de build et on le relocalise
#     (seules les bibliotheques absentes de la "excludelist" officielle
#     AppImage -- glibc, X11, Mesa/GL... -- sont embarquees ; celles-ci
#     doivent au contraire rester fournies par le systeme hote, c'est
#     la recommandation officielle du projet AppImage).
#   - opencv : on installe volontairement "opencv-python-headless" (et
#     non "opencv-python") pour EVITER d'avoir a embarquer libGL/libX11
#     et toute la pile graphique systeme, ce qui est explicitement
#     deconseille (drivers GPU proprietaires, casse la portabilite).
#     L'appli utilise tkinter pour son interface, pas les fenetres
#     opencv, donc ce choix est neutre fonctionnellement.
#
# Fichier de configuration
# -------------------------
# Par defaut chaque outil lit ./postcards.conf (repertoire courant).
# Les 4 points d'entree acceptent aussi :
#   ./ktmanager --conffile /chemin/vers/mon.conf
#   ./ktmanager -c /chemin/vers/mon.conf
# (kttools transmet nativement --conffile a son groupe click. Pour les
#  3 apps GUI, un lien symbolique "postcards.conf" est cree a cote du
#  fichier fourni -- afin que les chemins relatifs datadir/import/tmp/...
#  du fichier de conf restent coherents -- puis on s'y place avant de
#  lancer l'application, qui lit ensuite ./postcards.conf normalement).
#
# Prerequis sur la machine de build (Linux x86_64/aarch64)
# ----------------------------------------------------------
#   - python3 avec tkinter fonctionnel : sudo apt install python3-tk tcl8.6
#   - tesseract-ocr (pour l'embarquer) : sudo apt install tesseract-ocr \
#         tesseract-ocr-fra tesseract-ocr-eng
#   - pip, curl
#   - acces reseau a PyPI, GitHub (appimagetool) et, pour --gpu, a
#     download.pytorch.org
#   - libfuse2 recommande pour EXECUTER les AppImages produites
#
# Proxy (--proxy)
# -----------------
# --proxy fait passer TOUS les telechargements du build (pip/PyPI,
# torch, curl, appimagetool) par un proxy HTTP(S), squid local par
# defaut (http://127.0.0.1:3128). Utile derriere un cache/proxy
# d'entreprise ou pour eviter de re-telecharger les paquets a chaque
# build. Variable equivalente : PROXY_URL.
#
# Usage
# -----
#   ./build-appimage.sh                        # 1 AppImage, torch CPU
#   ./build-appimage.sh --gpu=nvidia            # variante NVIDIA/CUDA
#   ./build-appimage.sh --gpu=amd               # variante AMD/ROCm
#   ./build-appimage.sh --gpu=cpu,nvidia,amd    # les 3 en une fois
#   ./build-appimage.sh --no-tesseract          # sans OCR embarque
#   ./build-appimage.sh --no-similar            # sans torch/open-clip du tout
#   ./build-appimage.sh --no-travel              # sans ortools
#   ./build-appimage.sh --clean                 # nettoie build-appimage/
#   ./build-appimage.sh --proxy                 # via squid local (127.0.0.1:3128)
#   ./build-appimage.sh --proxy=http://10.0.0.1:3128
#   TESSERACT_LANGS=fra+eng+deu ./build-appimage.sh
#   CUDA_VERSION=cu126 ./build-appimage.sh --gpu=nvidia
#   ROCM_VERSION=rocm6.4 ./build-appimage.sh --gpu=amd
#   PYTHON_BIN=python3.12 ./build-appimage.sh
#
set -euo pipefail

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-${SCRIPT_DIR}}"
BUILD_DIR="${PROJECT_DIR}/build-appimage"
DIST_DIR="${PROJECT_DIR}/dist"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ARCH="$(uname -m)"
APP_ID="KartoTek"
TOOLS=(kttools ktmanager ktimport ktscan)
DEFAULT_TOOL="ktmanager"

WITH_TESSERACT=1
TESSERACT_BIN="${TESSERACT_BIN:-}"
TESSERACT_LANGS="${TESSERACT_LANGS:-fra+eng}"

# Proxy (squid par defaut) pour tous les telechargements du build : pip
# (PyPI, torch), curl (excludelist), python-appimage (appimagetool).
USE_PROXY=0
PROXY_URL="${PROXY_URL:-http://127.0.0.1:3128}"

WITH_SIMILAR=1
GPU_ARG="cpu"
CUDA_VERSION="${CUDA_VERSION:-cu128}"
ROCM_VERSION="${ROCM_VERSION:-rocm7.2}"
SIMILAR_TORCH_DEPS=(torch torchvision)
SIMILAR_DEPS=(requests pillow imagehash open-clip-torch)

WITH_TRAVEL=1
TRAVEL_DEPS=(ortools)

# Dependances pip explicites (equivalent de l'extra [tkinter] du
# pyproject.toml, mais avec opencv-python-headless a la place
# d'opencv-python -- voir explication en tete de fichier).
PIP_DEPS=(click pytesseract deskew numpy tqdm pyzstd paramiko pillow opencv-python-headless)

ICON_DIR="${PROJECT_DIR}/src/tkpostcards/images"
ICON_SRC="${ICON_DIR}/kartotek_tkinter_256.png"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERREUR:\033[0m %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --clean)
            log "Suppression de ${BUILD_DIR}"
            rm -rf "${BUILD_DIR}"
            exit 0
            ;;
        --no-tesseract) WITH_TESSERACT=0 ;;
        --no-similar) WITH_SIMILAR=0 ;;
        --no-travel) WITH_TRAVEL=0 ;;
        --gpu=*) GPU_ARG="${arg#--gpu=}" ;;
        --proxy) USE_PROXY=1 ;;
        --proxy=*) USE_PROXY=1; PROXY_URL="${arg#--proxy=}" ;;
        --no-proxy) USE_PROXY=0 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^#//'
            exit 0
            ;;
        *)
            die "Option inconnue: $arg (voir --help)"
            ;;
    esac
done

IFS=',' read -r -a GPU_VARIANTS <<< "${GPU_ARG}"
for v in "${GPU_VARIANTS[@]}"; do
    case "$v" in
        cpu|nvidia|amd) ;;
        *) die "Variante --gpu inconnue: '$v' (attendu: cpu, nvidia, amd)" ;;
    esac
done
if [ "$WITH_SIMILAR" -eq 0 ] && [ "${GPU_ARG}" != "cpu" ]; then
    warn "--gpu ignore car --no-similar est actif (pas de torch a installer)."
    GPU_VARIANTS=(cpu)
fi

torch_index_url() {
    case "$1" in
        cpu)    echo "https://download.pytorch.org/whl/cpu" ;;
        nvidia) echo "https://download.pytorch.org/whl/${CUDA_VERSION}" ;;
        amd)    echo "https://download.pytorch.org/whl/${ROCM_VERSION}" ;;
    esac
}

variant_suffix() {
    # Nomme les fichiers produits d'apres la version reelle de
    # CUDA/ROCm embarquee (ex: -cu128, -rocm7.2) plutot que le nom
    # generique de la variante, pour s'y retrouver quand plusieurs
    # versions sont testees.
    case "$1" in
        cpu)    echo "cpu" ;;
        nvidia) echo "${CUDA_VERSION}" ;;
        amd)    echo "${ROCM_VERSION}" ;;
    esac
}

if [ "$USE_PROXY" -eq 1 ]; then
    log "Utilisation du proxy pour les telechargements : ${PROXY_URL}"
    export http_proxy="${PROXY_URL}"
    export https_proxy="${PROXY_URL}"
    export HTTP_PROXY="${PROXY_URL}"
    export HTTPS_PROXY="${PROXY_URL}"
    # pip lit egalement ces variables, mais on le precise explicitement
    # pour eviter toute divergence de comportement selon les versions.
    export PIP_PROXY="${PROXY_URL}"
    if ! curl -sf --max-time 5 --proxy "${PROXY_URL}" -o /dev/null "https://pypi.org/simple/"; then
        warn "Le proxy ${PROXY_URL} ne semble pas joignable ou ne relaie pas pypi.org. On continue quand meme (--no-proxy pour desactiver)."
    fi
fi

# ----------------------------------------------------------------------
# Verifications prealables
# ----------------------------------------------------------------------
[ -f "${PROJECT_DIR}/pyproject.toml" ] || die \
    "pyproject.toml introuvable dans ${PROJECT_DIR}. Placez ce script a la racine du projet (ou definissez PROJECT_DIR)."

command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "python introuvable: $PYTHON_BIN"
command -v curl >/dev/null 2>&1 || die "curl est requis"
PYTHON_ABS="$(command -v "$PYTHON_BIN")"

if ! "$PYTHON_BIN" -c "import tkinter" >/dev/null 2>&1; then
    die "$PYTHON_BIN n'a pas de support tkinter. Installez-le, ex: sudo apt install python3-tk tcl8.6"
fi

if [ "$WITH_TESSERACT" -eq 1 ]; then
    if [ -z "$TESSERACT_BIN" ]; then
        TESSERACT_BIN="$(command -v tesseract || true)"
    fi
    if [ -z "$TESSERACT_BIN" ]; then
        warn "tesseract introuvable sur cette machine : l'OCR ne sera pas embarque (--no-tesseract pour supprimer cet avertissement, ou installez tesseract-ocr)."
        WITH_TESSERACT=0
    fi
fi

ensure_python_appimage() {
    # On verifie et on installe pour l'interpreteur $PYTHON_BIN precisement
    # (via "python -m python_appimage"), plutot que de se fier a un
    # eventuel executable "python-appimage" deja present dans le PATH
    # (qui pourrait appartenir a un tout autre python, avec des
    # dependances manquantes -- ex: "requires requests, which is not
    # installed").
    if "$PYTHON_BIN" -c "import python_appimage" >/dev/null 2>&1 && \
       "$PYTHON_BIN" -c "import requests" >/dev/null 2>&1; then
        return 0
    fi
    log "Installation de python-appimage (et de sa dependance requests) pour $PYTHON_BIN"
    "$PYTHON_BIN" -m pip install --user --upgrade python-appimage requests 2>&1 | tail -10 || \
        "$PYTHON_BIN" -m pip install --user --break-system-packages --upgrade python-appimage requests
    "$PYTHON_BIN" -c "import python_appimage" >/dev/null 2>&1 || die \
        "python_appimage reste introuvable pour $PYTHON_BIN apres installation."
    "$PYTHON_BIN" -c "import requests" >/dev/null 2>&1 || die \
        "requests (dependance de python-appimage) reste introuvable pour $PYTHON_BIN apres installation."
}
ensure_python_appimage
PYTHON_APPIMAGE_CLI=("$PYTHON_BIN" -m python_appimage)

mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

# ----------------------------------------------------------------------
# Etape 1 : image de base = python systeme (avec tkinter) relocalise
# (partagee par toutes les variantes GPU)
# ----------------------------------------------------------------------
PYFULLVER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
PYXY="$("$PYTHON_BIN" -c 'import sys; print("%d%d" % sys.version_info[:2])')"
PYVER="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
BASE_IMAGE_NAME="python${PYFULLVER}-cp${PYXY}-cp${PYXY}-manylinux_local_${ARCH}.AppImage"
BASE_IMAGE_PATH="${BUILD_DIR}/${BASE_IMAGE_NAME}"

if [ -f "${BASE_IMAGE_PATH}" ]; then
    log "Image de base deja construite : ${BASE_IMAGE_NAME}"
else
    log "Construction de l'image de base (python local + tkinter) : ${BASE_IMAGE_NAME}"
    ( cd "${BUILD_DIR}" && "${PYTHON_APPIMAGE_CLI[@]}" build local -p "$PYTHON_ABS" -d "${BASE_IMAGE_NAME}" )
fi
chmod +x "${BASE_IMAGE_PATH}"

EXCLUDELIST="${HOME}/.cache/python-appimage/share/excludelist"
if [ "$WITH_TESSERACT" -eq 1 ] && [ ! -f "${EXCLUDELIST}" ]; then
    mkdir -p "$(dirname "${EXCLUDELIST}")"
    curl -sL "https://raw.githubusercontent.com/probonopd/AppImages/master/excludelist" \
        -o "${EXCLUDELIST}" || warn "Impossible de recuperer l'excludelist, aucune bibliotheque exclue."
fi

bundle_lib_deps() {
    # Copie recursivement les dependances partagees d'un binaire ELF vers
    # $2 (usr/opt-lib), sauf celles listees dans l'excludelist (glibc,
    # X11, Mesa/GL... -- a NE PAS embarquer dans une AppImage).
    local target="$1" outlib="$2" seen="$3"
    [ -f "$target" ] || return 0
    local name path
    while IFS= read -r line; do
        name="$(awk '{print $1}' <<<"$line")"
        path="$(awk '{print $3}' <<<"$line")"
        [ -z "$name" ] && continue
        [ -z "$path" ] && continue
        [ "$path" = "not" ] && continue
        grep -qxF -- "$name" "${EXCLUDELIST}" 2>/dev/null && continue
        grep -qxF -- "$name" "$seen" 2>/dev/null && continue
        echo "$name" >> "$seen"
        if [ -f "$path" ]; then
            cp -n "$path" "${outlib}/" 2>/dev/null || true
            bundle_lib_deps "$path" "$outlib" "$seen"
        fi
    done < <(ldd "$target" 2>/dev/null | grep '=>')
}

# ========================================================================
# build_variant <variante gpu> <suffixe dist/desktop/AppImage>
# Construit une AppImage complete pour la variante donnee.
# ========================================================================
build_variant() {
    local VARIANT="$1"
    local SUFFIX="$2"
    local TORCH_INDEX_URL
    TORCH_INDEX_URL="$(torch_index_url "${VARIANT}")"

    log "=== Variante '${VARIANT}' (suffixe '${SUFFIX}') ==="

    local APPDIR="${BUILD_DIR}/AppDir${SUFFIX}"
    rm -rf "${APPDIR}" "${BUILD_DIR}/squashfs-root"
    log "Extraction de l'image de base"
    ( cd "${BUILD_DIR}" && "./${BASE_IMAGE_NAME}" --appimage-extract >/dev/null )
    mv "${BUILD_DIR}/squashfs-root" "${APPDIR}"

    local APPDIR_PY="${APPDIR}/opt/python${PYVER}/bin/python${PYVER}"
    [ -x "${APPDIR_PY}" ] || die "Interpreteur introuvable dans l'AppDir: ${APPDIR_PY}"

    # Empeche l'interpreteur relocalise de piocher dans le site-packages
    # "utilisateur" de la machine de build (~/.local/lib/pythonX.Y/
    # site-packages) : ce chemin est calcule a partir de $HOME et fuite
    # sinon dans l'AppDir independamment de son prefixe isole, ce qui
    # peut a la fois polluer les avertissements pip ("X requires Y, qui
    # n'est pas installe" pour des paquets hote sans rapport, ex.
    # python-appimage) et, plus grave, faire utiliser silencieusement
    # de mauvaises versions de paquets lors du build. On le scope aux
    # seuls appels a APPDIR_PY (pas d'"export" global : ca casserait
    # ensuite l'appel a python-appimage installe en --user sur l'hote).
    local APPDIR_PY_RUN=(env PYTHONNOUSERSITE=1 "${APPDIR_PY}")

    # --- bootstrap de pip dans l'AppDir ---
    if ! "${APPDIR_PY_RUN[@]}" -m pip --version >/dev/null 2>&1; then
        log "Bootstrap de pip dans l'AppDir"
        if ! "${APPDIR_PY_RUN[@]}" -m ensurepip --upgrade >/tmp/ensurepip.$$.log 2>&1; then
            # Debian/Ubuntu desactive ensurepip pour le python systeme et
            # place pip dans dist-packages plutot que dans la lib copiee
            # par python-appimage : on le recopie manuellement.
            local PURELIB FOUND CAND
            PURELIB="$("${APPDIR_PY_RUN[@]}" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
            FOUND=0
            for CAND in /usr/lib/python3/dist-packages "$("$PYTHON_ABS" -c 'import sysconfig; print(sysconfig.get_paths().get("platlib",""))' 2>/dev/null)"; do
                if [ -n "$CAND" ] && [ -d "${CAND}/pip" ]; then
                    mkdir -p "$PURELIB"
                    cp -r "${CAND}/pip" "$PURELIB/"
                    cp -r "${CAND}"/pip-*.dist-info "$PURELIB/" 2>/dev/null || true
                    FOUND=1
                    break
                fi
            done
            [ "$FOUND" -eq 1 ] || die "Impossible d'installer pip dans l'AppDir. Installez python3-pip sur la machine de build."
        fi
    fi
    "${APPDIR_PY_RUN[@]}" -m pip --version >/dev/null 2>&1 || die "pip toujours indisponible dans l'AppDir"

    local PIP_EXTRA_ARGS=()
    if [ -f "${APPDIR}/opt/python${PYVER}/lib/python${PYVER}/EXTERNALLY-MANAGED" ] || \
       [ -f "$("${APPDIR_PY_RUN[@]}" -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')/EXTERNALLY-MANAGED" ]; then
        PIP_EXTRA_ARGS+=(--break-system-packages)
    fi
    local PIP_INSTALL=("${APPDIR_PY_RUN[@]}" -m pip install --no-warn-script-location --upgrade "${PIP_EXTRA_ARGS[@]}")

    # --- installation du projet + dependances ---
    log "Installation de pypostcards (sans dependances)"
    "${PIP_INSTALL[@]}" -q "${PROJECT_DIR}" --no-deps

    log "Installation des dependances (${PIP_DEPS[*]})"
    "${PIP_INSTALL[@]}" -q "${PIP_DEPS[@]}"

    if [ "$WITH_SIMILAR" -eq 1 ]; then
        log "Installation de torch/torchvision [${VARIANT}] (${TORCH_INDEX_URL})"
        "${PIP_INSTALL[@]}" -q --index-url "${TORCH_INDEX_URL}" "${SIMILAR_TORCH_DEPS[@]}"
        log "Installation des dependances 'similar' (${SIMILAR_DEPS[*]})"
        "${PIP_INSTALL[@]}" -q "${SIMILAR_DEPS[@]}"
    else
        log "Fonctionnalite 'similar' non embarquee (--no-similar)"
    fi

    if [ "$WITH_TRAVEL" -eq 1 ]; then
        log "Installation des dependances 'travel' (${TRAVEL_DEPS[*]})"
        "${PIP_INSTALL[@]}" -q "${TRAVEL_DEPS[@]}"
    else
        log "Fonctionnalite 'travel' non embarquee (--no-travel)"
    fi

    # --- localisation des scripts installes (kttools, ktmanager, ...) ---
    local KTTOOLS_PATH BINDIR_ABS BINDIR_REL
    KTTOOLS_PATH="$(find "${APPDIR}/opt" -maxdepth 5 -type f -name kttools 2>/dev/null | head -1 || true)"
    [ -n "${KTTOOLS_PATH}" ] || die "kttools introuvable apres installation : le paquet pypostcards s'est-il installe correctement ?"
    BINDIR_ABS="$(dirname "${KTTOOLS_PATH}")"
    BINDIR_REL="${BINDIR_ABS#${APPDIR}/}"
    log "Scripts installes dans : ${BINDIR_REL}"
    local t
    for t in "${TOOLS[@]}"; do
        [ -f "${BINDIR_ABS}/${t}" ] || die "Le point d'entree '${t}' est absent de ${BINDIR_ABS}"
    done

    # --- bundle de tesseract (optionnel) ---
    mkdir -p "${APPDIR}/usr/opt-bin" "${APPDIR}/usr/opt-lib" "${APPDIR}/usr/share/tessdata"
    if [ "$WITH_TESSERACT" -eq 1 ]; then
        log "Embarquement de tesseract (${TESSERACT_BIN})"
        cp "${TESSERACT_BIN}" "${APPDIR}/usr/opt-bin/tesseract"
        chmod +x "${APPDIR}/usr/opt-bin/tesseract"
        local SEEN_LIBS_FILE NLIBS
        SEEN_LIBS_FILE="$(mktemp)"
        bundle_lib_deps "${TESSERACT_BIN}" "${APPDIR}/usr/opt-lib" "${SEEN_LIBS_FILE}"
        NLIBS="$(wc -l < "${SEEN_LIBS_FILE}")"
        rm -f "${SEEN_LIBS_FILE}"
        log "  -> ${NLIBS} bibliotheques embarquees pour tesseract"

        local LANGS lang tessdatadir found TRAINEDDATA FOUND_LANG
        IFS='+,' read -r -a LANGS <<< "${TESSERACT_LANGS}"
        FOUND_LANG=0
        for lang in "${LANGS[@]}"; do
            TRAINEDDATA=""
            for tessdatadir in /usr/share/tesseract-ocr /usr/share/tessdata; do
                [ -d "${tessdatadir}" ] || continue
                found="$(find "${tessdatadir}" -iname "${lang}.traineddata" 2>/dev/null | head -1 || true)"
                if [ -n "${found}" ]; then
                    TRAINEDDATA="${found}"
                    break
                fi
            done
            if [ -n "${TRAINEDDATA}" ]; then
                cp "${TRAINEDDATA}" "${APPDIR}/usr/share/tessdata/"
                FOUND_LANG=1
            else
                warn "Donnees de langue tesseract introuvables pour '${lang}' (paquet tesseract-ocr-${lang} manquant ?)"
            fi
        done
        [ "$FOUND_LANG" -eq 1 ] || warn "Aucune donnee de langue tesseract embarquee : l'OCR ne fonctionnera pas."
    else
        log "OCR non embarque (--no-tesseract ou tesseract absent)"
    fi

    # --- AppRun (dispatcher unique) ---
    log "Generation du AppRun"
    rm -f "${APPDIR}/AppRun"
    cat > "${APPDIR}/AppRun" <<EOF
#! /bin/bash
# AppRun genere par build-appimage.sh - dispatcher pour ${TOOLS[*]}
# Variante GPU : ${VARIANT}
set -e

if [ -z "\${APPIMAGE:-}" ]; then
    export ARGV0="\$0"
    self=\$(readlink -f -- "\$0")
    export APPDIR="\${self%/*}"
fi

# N'utilise jamais le site-packages "utilisateur" (~/.local/lib/...) de
# la machine qui execute l'AppImage : cela garantit que seuls les
# paquets embarques ici sont utilises, quels que soient les paquets
# Python que l'utilisateur final aurait pu installer par ailleurs.
export PYTHONNOUSERSITE=1

# --- Tcl/Tk (interfaces graphiques) ---
export TCL_LIBRARY="\${APPDIR}/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="\${APPDIR}/usr/share/tcltk/tk8.6"
export TKPATH="\${TK_LIBRARY}"

# --- binaires/bibliotheques additionnels embarques (tesseract, ...) ---
export PATH="\${APPDIR}/usr/opt-bin:\${PATH}"
export LD_LIBRARY_PATH="\${APPDIR}/usr/opt-lib:\${LD_LIBRARY_PATH:-}"
if [ -d "\${APPDIR}/usr/share/tessdata" ] && [ -n "\$(ls -A "\${APPDIR}/usr/share/tessdata" 2>/dev/null)" ]; then
    export TESSDATA_PREFIX="\${APPDIR}/usr/share/tessdata"
fi

PYTHON="\${APPDIR}/opt/python${PYVER}/bin/python${PYVER}"
BINDIR="\${APPDIR}/${BINDIR_REL}"

# --- selection de l'outil : nom d'invocation (symlink), sinon 1er argument ---
TOOL="\$(basename -- "\${ARGV0:-\$0}")"
case "\${TOOL}" in
    kttools|ktmanager|ktimport|ktscan) ;;
    *)
        if [ "\$#" -ge 1 ]; then
            case "\$1" in
                kttools|ktmanager|ktimport|ktscan)
                    TOOL="\$1"; shift ;;
            esac
        fi
        ;;
esac
case "\${TOOL}" in
    kttools|ktmanager|ktimport|ktscan) ;;
    *) TOOL="${DEFAULT_TOOL}" ;;
esac

BIN="\${BINDIR}/\${TOOL}"

if [ "\${TOOL}" = "kttools" ]; then
    # kttools est directement le groupe click : --conffile est gere en natif.
    exec "\${PYTHON}" "\${BIN}" "\$@"
fi

# ktmanager / ktimport / ktscan : leur run() insere "main" dans argv, donc
# --conffile/-c doit etre intercepte ici (l'appli lit ./postcards.conf
# dans le repertoire courant par defaut).
CONF="postcards.conf"
ARGS=()
while [ "\$#" -gt 0 ]; do
    case "\$1" in
        --conffile|-c) CONF="\$2"; shift 2 ;;
        --conffile=*) CONF="\${1#*=}"; shift ;;
        *) ARGS+=("\$1"); shift ;;
    esac
done

if [ -f "\${CONF}" ]; then
    CONF="\$(readlink -f "\${CONF}")"
    CONFDIR="\$(dirname -- "\${CONF}")"
    if [ "\$(basename -- "\${CONF}")" != "postcards.conf" ]; then
        # L'appli lit toujours ./postcards.conf : on cree un lien a cote
        # du fichier fourni, dans son propre repertoire, pour que les
        # chemins relatifs datadir/import/tmp/... restent coherents.
        ln -sf "\${CONF}" "\${CONFDIR}/postcards.conf"
    fi
    cd "\${CONFDIR}"
elif [ "\${CONF}" != "postcards.conf" ]; then
    echo "\${TOOL}: fichier de configuration '\${CONF}' introuvable" >&2
    exit 1
fi

exec "\${PYTHON}" "\${BIN}" "\${ARGS[@]}"
EOF
    chmod +x "${APPDIR}/AppRun"

    # --- desktop + icone ---
    # appimagetool exige que l'icone referencee par Icon= existe
    # reellement dans l'AppDir (sinon le build echoue) : on conserve
    # donc l'icone python par defaut de l'image de base comme dernier
    # recours avant de la supprimer.
    local DEFAULT_ICON DESKTOP_ICON
    DEFAULT_ICON="$(find "${APPDIR}" -maxdepth 1 -iname '*.png' 2>/dev/null | head -1 || true)"
    rm -f "${APPDIR}"/*.desktop "${APPDIR}"/*.appdata.xml
    if [ -f "${ICON_SRC}" ]; then
        rm -f "${APPDIR}"/*.png
        cp "${ICON_SRC}" "${APPDIR}/${APP_ID}.png"
        DESKTOP_ICON="${APP_ID}"
    elif [ -f "${ICON_DIR}/ktmanager_256.png" ]; then
        warn "Icone '${ICON_SRC}' introuvable, repli sur ${ICON_DIR}/ktmanager_256.png."
        rm -f "${APPDIR}"/*.png
        cp "${ICON_DIR}/ktmanager_256.png" "${APPDIR}/${APP_ID}.png"
        DESKTOP_ICON="${APP_ID}"
    elif [ -n "${DEFAULT_ICON}" ]; then
        warn "Icone introuvable (${ICON_SRC}), on garde l'icone Python par defaut de l'image de base."
        DESKTOP_ICON="$(basename "${DEFAULT_ICON}" .png)"
    else
        warn "Aucune icone disponible : le champ Icon sera omis du .desktop."
        DESKTOP_ICON=""
    fi
    cat > "${APPDIR}/${APP_ID}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=${APP_ID}${SUFFIX}
Comment=Gestion de collection de cartes postales (kttools, ktmanager, ktimport, ktscan)
Exec=ktmanager
$( [ -n "${DESKTOP_ICON}" ] && echo "Icon=${DESKTOP_ICON}" )
Categories=Graphics;Office;
Terminal=false
EOF

    # --- empaquetage avec appimagetool ---
    log "Empaquetage de l'AppImage (${VARIANT})"
    local APPIMAGETOOL OUTPUT
    APPIMAGETOOL="$("$PYTHON_BIN" -c 'from python_appimage.utils.deps import ensure_appimagetool; print(ensure_appimagetool())')"
    OUTPUT="${DIST_DIR}/${APP_ID}${SUFFIX}-${ARCH}.AppImage"
    rm -f "${OUTPUT}"
    ARCH="${ARCH}" "${APPIMAGETOOL}" --no-appstream "${APPDIR}" "${OUTPUT}"
    chmod +x "${OUTPUT}"

    # --- symlinks de confort ---
    for t in "${TOOLS[@]}"; do
        ln -sf "$(basename "${OUTPUT}")" "${DIST_DIR}/${t}${SUFFIX}"
    done

    echo "${OUTPUT}" >> "${BUILD_DIR}/.last_outputs"
}

# ----------------------------------------------------------------------
# Construction de chaque variante demandee
# ----------------------------------------------------------------------
rm -f "${BUILD_DIR}/.last_outputs"
if [ "${#GPU_VARIANTS[@]}" -eq 1 ] && [ "${GPU_VARIANTS[0]}" = "cpu" ]; then
    # variante unique et par defaut : nom de fichier inchange (compat.)
    build_variant "cpu" ""
else
    for variant in "${GPU_VARIANTS[@]}"; do
        build_variant "${variant}" "-$(variant_suffix "${variant}")"
    done
fi

if [ -f "${PROJECT_DIR}/postcards.conf" ]; then
    cp "${PROJECT_DIR}/postcards.conf" "${DIST_DIR}/postcards.conf.sample"
fi

log "Termine."
echo
while IFS= read -r out; do
    echo "  ${out}"
done < "${BUILD_DIR}/.last_outputs"
echo
echo "Exemples :"
echo "  ${DIST_DIR}/ktmanager           # (ou ktmanager-<CUDA_VERSION> / ktmanager-<ROCM_VERSION> selon --gpu)"
echo "  ${DIST_DIR}/kttools --conffile /chemin/vers/postcards.conf list"
