<#
.SYNOPSIS
    install_KartoTek.ps1
    -----------------------------------------------------------------------
    Installe PyPostcards (KartoTek) depuis PyPI dans un environnement
    virtuel Python, sous Windows.
    Installs PyPostcards (KartoTek) from PyPI inside a Python virtual
    environment, on Windows.

.DESCRIPTION
    Portage Windows (PowerShell) du script install_KartoTek.sh (bash/Linux).
    Windows (PowerShell) port of the install_KartoTek.sh (bash/Linux) script.

.PARAMETER Install
    Installe (ou réinstalle) l'application (option par défaut).

.PARAMETER Update
    Met à jour l'application vers la dernière version depuis PyPI.

.PARAMETER UpdateComplete
    Comme -Update, et met aussi à jour TOUS les paquets du venv.

.PARAMETER Uninstall
    Désinstalle l'application (venv, lanceurs, raccourcis, icônes).

.PARAMETER Version
    Affiche le numéro de version du script.

.PARAMETER Help
    Affiche l'aide.

.EXAMPLE
    .\install_KartoTek.ps1
    .\install_KartoTek.ps1 -Update
    .\install_KartoTek.ps1 -UpdateComplete
    .\install_KartoTek.ps1 -Uninstall

.NOTES
    A exécuter dans une fenêtre PowerShell normale (pas besoin d'être
    administrateur, sauf éventuellement pour l'installation de Python
    ou Tesseract via winget selon la configuration du poste).
    Si l'exécution de scripts est bloquée, lancez d'abord :
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>

[CmdletBinding(DefaultParameterSetName = 'Install')]
param(
    [Parameter(ParameterSetName = 'Install')]
    [switch]$Install,

    [Parameter(ParameterSetName = 'Update')]
    [switch]$Update,

    [Parameter(ParameterSetName = 'Update')]
    [Alias('UpdateAll')]
    [switch]$UpdateComplete,

    [Parameter(ParameterSetName = 'Uninstall')]
    [switch]$Uninstall,

    [Parameter(ParameterSetName = 'Version')]
    [Alias('v')]
    [switch]$Version,

    [Parameter(ParameterSetName = 'Help')]
    [Alias('h')]
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

# =============================================================================
# VERSION DU SCRIPT
# -----------------------------------------------------------------------------
# IMPORTANT : incrémentez le numéro MINEUR (X.Y.Z -> X.Y+1.Z) à CHAQUE
# modification de ce script, aussi petite soit-elle. Le numéro MAJEUR est
# réservé aux changements de comportement importants ou aux ruptures de
# compatibilité. Gardez ce numéro aligné avec install_KartoTek.sh dans la
# mesure du possible.
# =============================================================================
$ScriptVersion = "1.0.0"

# =============================================================================
# CONFIGURATION (modifiable facilement)
# =============================================================================

$PackageName = "pypostcards"
$AppName     = "KartoTek"

# Emplacement du venv (surchargeable via variable d'environnement)
$VenvDir = if ($env:PYPOSTCARDS_VENV_DIR) { $env:PYPOSTCARDS_VENV_DIR } else { Join-Path $env:LOCALAPPDATA "PyPostcards\venv" }

# Emplacement du fichier de configuration
$ConfDir  = Join-Path $env:LOCALAPPDATA "PyPostcards"
$ConfFile = Join-Path $ConfDir "postcards.conf"

# Où poser les lanceurs (.cmd) et les raccourcis
$BinDir     = Join-Path $env:LOCALAPPDATA "PyPostcards\bin"
$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName"
$IconDir    = Join-Path $env:LOCALAPPDATA "PyPostcards\icons"

# Scripts / entry points fournis par le paquet (identiques à la version Linux)
$EntryPoints = [ordered]@{
    kttools   = "tkpostcards.scripts.tktools:cli"
    ktmanager = "tkpostcards.tkmanager:run"
    ktimport  = "tkpostcards.tkimport:run"
    ktscan    = "tkpostcards.tkscan:run"
}
$ScriptOrder = @("kttools", "ktmanager", "ktimport", "ktscan")

# Scripts pour lesquels on crée un raccourci (GUI). kttools est un CLI.
$DesktopScripts = @("ktmanager", "ktimport", "ktscan")

# --- Langues Tesseract ------------------------------------------------------
$TesseractLangsDefault   = @("fra", "eng")
$TesseractLangsAvailable = @("eng", "fra", "deu")

# =============================================================================
# INTERNATIONALISATION / I18N
# =============================================================================

function Get-DetectedLang {
    $probe = @($env:LANGUAGE, $env:LC_ALL, $env:LANG, (Get-Culture).Name) | Where-Object { $_ }
    foreach ($p in $probe) {
        if ($p -match '^(fr)') { return "fr" }
    }
    return "en"
}
$LangCode = Get-DetectedLang

$MsgFr = @{
    title                      = "=== Installation de {0} (paquet PyPI : {1}) ==="
    checking_tools             = "Verification des outils requis (Python 3, venv, pip)..."
    tool_missing               = "Outil manquant : {0}"
    propose_install_tools      = "Certains outils sont manquants. Tenter de les installer maintenant avec winget ? [o/N] "
    tools_ok                   = "Tous les outils necessaires sont presents."
    no_pkg_manager             = "winget n'est pas disponible sur ce poste. Installez manuellement : {0}"
    install_cancelled          = "Installation annulee par l'utilisateur."
    choose_variant             = "Quelle version installer ?"
    variant_light              = "Legere (ktmanager, ktimport)"
    variant_full               = "Complete (+ ocr, similar, travel)"
    choose_prompt              = "Votre choix [1/2] : "
    creating_venv              = "Creation de l'environnement virtuel dans {0} ..."
    venv_exists                = "Un venv existe deja dans {0}, il sera reutilise."
    installing_pkg             = "Installation de {0} (extra : {1}) depuis PyPI..."
    checking_tesseract         = "Verification de la presence de Tesseract..."
    tesseract_found            = "Tesseract est deja installe ({0})."
    tesseract_missing          = "Tesseract n'est pas installe mais est requis pour l'OCR."
    propose_tesseract_install  = "Installer Tesseract OCR maintenant via winget ? [o/N] "
    tesseract_skip_warning     = "ATTENTION : sans Tesseract, les fonctionnalites OCR ne fonctionneront pas."
    tesseract_lang_note        = "Note : l'installeur Windows de Tesseract propose le choix des langues pendant son installation (fra, eng, deu, ...). Si une langue manque apres coup, relancez l'installeur Tesseract ou ajoutez le fichier .traineddata correspondant dans le dossier tessdata."
    checking_scanimage         = "Verification du support de numerisation..."
    scanimage_note             = "Sous Windows, la numerisation utilise les pilotes WIA/TWAIN de votre scanner (pas d'equivalent de sane-utils a installer via ce script). Assurez-vous que le pilote de votre scanner est installe."
    creating_conf              = "Creation du fichier de configuration dans {0} ..."
    conf_exists                = "Un fichier de configuration existe deja dans {0}, il est conserve tel quel."
    creating_data_dir          = "Creation du dossier manquant : {0}"
    summary_conf               = "Fichier de configuration : {0}"
    creating_launchers         = "Creation des lanceurs dans {0} ..."
    creating_shortcuts         = "Creation des raccourcis dans {0} ..."
    icon_not_found             = "Aucune icone trouvee dans le paquet, l'icone par defaut sera utilisee."
    path_reminder              = "Remarque : {0} a ete ajoute a votre PATH utilisateur. Ouvrez une NOUVELLE fenetre PowerShell pour que ce changement soit pris en compte."
    done                       = "Installation terminee !"
    summary_light              = "Version installee : legere (ktmanager, ktimport)"
    summary_full               = "Version installee : complete (ktmanager, ktimport, ocr, similar, travel)"
    summary_langs              = "Langues Tesseract souhaitees : {0}"
    summary_launch             = "Vous pouvez lancer les outils avec : {0}"
    yes_no_hint                = "(o = oui / n = non)"
    version_line                = "{0} - version du script : {1}"
    update_title               = "=== Mise a jour de {0} ==="
    uninstall_title            = "=== Desinstallation de {0} ==="
    update_no_install          = "Aucune installation existante trouvee dans {0}. Lancez d'abord : .\install_KartoTek.ps1 -Install"
    uninstall_no_install       = "Aucune installation existante trouvee. Rien a desinstaller."
    updating_pkg               = "Mise a jour de {0} (extra : {1}) depuis PyPI..."
    updating_all_pkgs          = "Mise a jour complete : mise a jour de tous les paquets du venv (au-dela de {0})..."
    no_outdated_pkgs           = "Tous les paquets du venv sont deja a jour."
    update_done                = "Mise a jour terminee !"
    update_all_done            = "Mise a jour complete terminee !"
    uninstall_removing_venv    = "Suppression de l'environnement virtuel : {0}"
    uninstall_removing_launchers = "Suppression des lanceurs dans {0}"
    uninstall_removing_shortcuts = "Suppression des raccourcis dans {0}"
    uninstall_removing_icons   = "Suppression des icones dans {0}"
    uninstall_keep_pkg_note    = "Note : les logiciels systeme (Python, Tesseract, ...) installes via winget ne sont PAS supprimes. Desinstallez-les manuellement si besoin."
    ask_remove_conf            = "Supprimer aussi le fichier de configuration ({0}) ? [o/N] "
    conf_removed                = "Fichier de configuration supprime : {0}"
    conf_kept                  = "Fichier de configuration conserve : {0}"
    data_dirs_kept_note        = "Remarque : vos donnees (cartes, images, dans {0}) n'ont PAS ete supprimees."
    uninstall_done              = "Desinstallation terminee."
}

$MsgEn = @{
    title                      = "=== {0} installation (PyPI package: {1}) ==="
    checking_tools             = "Checking required tools (Python 3, venv, pip)..."
    tool_missing               = "Missing tool: {0}"
    propose_install_tools      = "Some tools are missing. Try to install them now with winget? [y/N] "
    tools_ok                   = "All required tools are present."
    no_pkg_manager             = "winget is not available on this machine. Please install manually: {0}"
    install_cancelled          = "Installation cancelled by user."
    choose_variant             = "Which version do you want to install?"
    variant_light              = "Light (ktmanager, ktimport)"
    variant_full               = "Full (+ ocr, similar, travel)"
    choose_prompt              = "Your choice [1/2]: "
    creating_venv              = "Creating virtual environment in {0} ..."
    venv_exists                = "A venv already exists in {0}, it will be reused."
    installing_pkg             = "Installing {0} (extra: {1}) from PyPI..."
    checking_tesseract         = "Checking for Tesseract..."
    tesseract_found            = "Tesseract is already installed ({0})."
    tesseract_missing          = "Tesseract is not installed but is required for OCR."
    propose_tesseract_install  = "Install Tesseract OCR now via winget? [y/N] "
    tesseract_skip_warning     = "WARNING: without Tesseract, OCR features will not work."
    tesseract_lang_note        = "Note: the Windows Tesseract installer lets you pick languages during setup (fra, eng, deu, ...). If a language is missing afterwards, re-run the Tesseract installer or add the matching .traineddata file to the tessdata folder."
    checking_scanimage         = "Checking scanning support..."
    scanimage_note             = "On Windows, scanning uses your scanner's WIA/TWAIN drivers (no sane-utils equivalent is installed by this script). Make sure your scanner's driver is installed."
    creating_conf              = "Creating configuration file in {0} ..."
    conf_exists                = "A configuration file already exists in {0}, it is kept as is."
    creating_data_dir          = "Creating missing folder: {0}"
    summary_conf               = "Configuration file: {0}"
    creating_launchers         = "Creating launchers in {0} ..."
    creating_shortcuts         = "Creating shortcuts in {0} ..."
    icon_not_found             = "No icon found in the package, the default icon will be used."
    path_reminder              = "Note: {0} was added to your user PATH. Open a NEW PowerShell window for this to take effect."
    done                       = "Installation complete!"
    summary_light              = "Installed version: light (ktmanager, ktimport)"
    summary_full               = "Installed version: full (ktmanager, ktimport, ocr, similar, travel)"
    summary_langs              = "Requested Tesseract languages: {0}"
    summary_launch             = "You can launch the tools with: {0}"
    yes_no_hint                = "(y = yes / n = no)"
    version_line                = "{0} - script version: {1}"
    update_title               = "=== Updating {0} ==="
    uninstall_title            = "=== Uninstalling {0} ==="
    update_no_install          = "No existing installation found in {0}. Run: .\install_KartoTek.ps1 -Install first"
    uninstall_no_install       = "No existing installation found. Nothing to uninstall."
    updating_pkg               = "Updating {0} (extra: {1}) from PyPI..."
    updating_all_pkgs          = "Full update: updating every package in the venv (beyond {0})..."
    no_outdated_pkgs           = "All packages in the venv are already up to date."
    update_done                = "Update complete!"
    update_all_done            = "Full update complete!"
    uninstall_removing_venv    = "Removing virtual environment: {0}"
    uninstall_removing_launchers = "Removing launchers from {0}"
    uninstall_removing_shortcuts = "Removing shortcuts from {0}"
    uninstall_removing_icons   = "Removing icons from {0}"
    uninstall_keep_pkg_note    = "Note: system software (Python, Tesseract, ...) installed via winget is NOT removed. Uninstall it manually if needed."
    ask_remove_conf            = "Also remove the configuration file ({0})? [y/N] "
    conf_removed                = "Configuration file removed: {0}"
    conf_kept                  = "Configuration file kept: {0}"
    data_dirs_kept_note        = "Note: your data (cards, images, in {0}) was NOT removed."
    uninstall_done              = "Uninstall complete."
}

function Msg([string]$Key, [object[]]$FormatArgs = @()) {
    $table = if ($LangCode -eq 'fr') { $MsgFr } else { $MsgEn }
    $fmt = if ($table.ContainsKey($Key)) { $table[$Key] } else { $Key }
    if ($FormatArgs.Count -gt 0) { return [string]::Format($fmt, $FormatArgs) }
    return $fmt
}
function MsgLn([string]$Key, [object[]]$FormatArgs = @()) { Write-Host (Msg $Key $FormatArgs) }

function Ask-YesNo([string]$PromptKey, [object[]]$FormatArgs = @()) {
    $reply = Read-Host -Prompt (Msg $PromptKey $FormatArgs)
    return ($reply -match '^[oOyY]')
}

# =============================================================================
# ANALYSE DES ARGUMENTS / ARGUMENT PARSING
# =============================================================================

function Print-Usage {
    MsgLn 'version_line' @($AppName, $ScriptVersion)
    Write-Host ""
    Write-Host "Usage : .\install_KartoTek.ps1 [-Install|-Update|-UpdateComplete|-Uninstall|-Version|-Help]"
    Write-Host ""
    Write-Host "  -Install          Installe (ou reinstalle) $AppName (option par defaut)"
    Write-Host "  -Update           Met a jour $AppName vers la derniere version depuis PyPI"
    Write-Host "  -UpdateComplete   Comme -Update, et met aussi a jour TOUS les paquets du venv"
    Write-Host "  -Uninstall        Desinstalle $AppName (venv, lanceurs, raccourcis, icones)"
    Write-Host "  -Version          Affiche le numero de version du script"
    Write-Host "  -Help             Affiche cette aide"
}

if ($Version) { MsgLn 'version_line' @($AppName, $ScriptVersion); exit 0 }
if ($Help)    { Print-Usage; exit 0 }

$Action = 'install'
$UpdateMode = 'normal'
if ($Update)         { $Action = 'update';   $UpdateMode = 'normal' }
if ($UpdateComplete) { $Action = 'update';   $UpdateMode = 'complete' }
if ($Uninstall)      { $Action = 'uninstall' }

# =============================================================================
# DETECTION DE WINGET (gestionnaire de paquets Windows)
# =============================================================================

$HasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

function Install-WithWinget([string]$WingetId, [string]$FriendlyName) {
    if (-not $HasWinget) { return $false }
    Write-Host "winget install --id $WingetId -e --source winget"
    winget install --id $WingetId -e --source winget --accept-source-agreements --accept-package-agreements
    return $LASTEXITCODE -eq 0
}

# Detecte le lanceur Python a utiliser (py -3 en priorite sur Windows, sinon python)
function Get-PythonCmd {
    if (Get-Command py -ErrorAction SilentlyContinue) { return @('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @('python') }
    return $null
}

function Pip-InstallPkgFresh([string[]]$PyCmd, [string]$Spec) {
    # Etape 1 : paquet principal seul, forcé depuis PyPI, cache ignoré.
    & $PyCmd[0] $PyCmd[1..($PyCmd.Length-1)] -m pip install --no-cache-dir --force-reinstall --no-deps --upgrade $Spec
    if ($LASTEXITCODE -ne 0) { throw "pip install (no-deps) failed for $Spec" }
    # Etape 2 : installation normale des dependances (cache autorise).
    & $PyCmd[0] $PyCmd[1..($PyCmd.Length-1)] -m pip install $Spec
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for $Spec" }
}

function Get-PipExtraForVariant([string]$Variant) {
    if ($Variant -eq 'full') { return 'ktmanager,ktimport,ocr,similar,travel' }
    return 'ktmanager,ktimport'
}

# =============================================================================
# GESTION DU PATH UTILISATEUR
# =============================================================================

function Add-ToUserPath([string]$Dir) {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($current) { $parts = $current -split ';' }
    if ($parts -notcontains $Dir) {
        $new = if ($current) { "$current;$Dir" } else { $Dir }
        [Environment]::SetEnvironmentVariable('Path', $new, 'User')
        MsgLn 'path_reminder' @($Dir)
    }
}

# =============================================================================
# LANCEURS ET RACCOURCIS (fonctions reutilisees par install et update)
# =============================================================================

function Ensure-DataDirs([string]$ConfFilePath) {
    if (-not (Test-Path $ConfFilePath)) { return }
    $lines = Get-Content $ConfFilePath
    $dirs = @()
    foreach ($key in @('datadir', 'importdir', 'tmpdir')) {
        $line = $lines | Where-Object { $_ -match "^\s*$key\s*=" } | Select-Object -First 1
        if ($line) {
            $value = ($line -split '=', 2)[1].Trim()
            if ($value) { $dirs += $value }
        }
    }
    foreach ($dir in $dirs) {
        if (-not (Test-Path $dir)) {
            MsgLn 'creating_data_dir' @($dir)
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
    }
}

function New-Launchers {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    MsgLn 'creating_launchers' @($BinDir)

    foreach ($script in $ScriptOrder) {
        $venvExe = Join-Path $VenvDir "Scripts\$script.exe"
        if (Test-Path $venvExe) {
            $launcher = Join-Path $BinDir "$script.cmd"
            @"
@echo off
"$venvExe" --conffile "$ConfFile" %*
"@ | Set-Content -Path $launcher -Encoding ASCII
        }
    }
}

function New-Shortcuts {
    New-Item -ItemType Directory -Path $IconDir -Force | Out-Null
    New-Item -ItemType Directory -Path $StartMenuDir -Force | Out-Null
    MsgLn 'creating_shortcuts' @($StartMenuDir)

    # Recherche d'une icone fournie par le paquet installe (.ico de preference)
    $pyCmd = Get-PythonCmd
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    $pkgDataDir = $null
    if (Test-Path $venvPython) {
        $probe = @'
import importlib.util
spec = importlib.util.find_spec("tkpostcards")
if spec and spec.submodule_search_locations:
    print(list(spec.submodule_search_locations)[0])
'@
        try {
            $pkgDataDir = & $venvPython -c $probe 2>$null
        } catch { $pkgDataDir = $null }
    }

    $foundIcon = $null
    if ($pkgDataDir -and (Test-Path $pkgDataDir)) {
        $foundIcon = Get-ChildItem -Path $pkgDataDir -Recurse -Include *.ico, *.png -ErrorAction SilentlyContinue | Select-Object -First 1
    }

    $iconPath = $null
    if ($foundIcon) {
        $dest = Join-Path $IconDir $foundIcon.Name
        Copy-Item $foundIcon.FullName $dest -Force
        $iconPath = $dest
    } else {
        MsgLn 'icon_not_found'
    }

    $AppTitleFr = @{ ktmanager = "$AppName - Gestionnaire"; ktimport = "$AppName - Import"; ktscan = "$AppName - Numerisation" }
    $AppTitleEn = @{ ktmanager = "$AppName - Manager";      ktimport = "$AppName - Import"; ktscan = "$AppName - Scan" }

    $wsh = New-Object -ComObject WScript.Shell
    foreach ($script in $DesktopScripts) {
        $launcher = Join-Path $BinDir "$script.cmd"
        if (-not (Test-Path $launcher)) { continue }

        $title = if ($LangCode -eq 'fr') { $AppTitleFr[$script] } else { $AppTitleEn[$script] }
        $lnkPath = Join-Path $StartMenuDir "$title.lnk"
        $shortcut = $wsh.CreateShortcut($lnkPath)
        $shortcut.TargetPath = $launcher
        $shortcut.WorkingDirectory = $BinDir
        if ($iconPath -and $iconPath.EndsWith('.ico')) {
            $shortcut.IconLocation = $iconPath
        }
        $shortcut.Save()
    }
}

# =============================================================================
# do_install : sequence complete d'installation
# =============================================================================

function Do-Install {

    # --- ETAPE 1 : outils requis (Python, venv, pip) ------------------------
    MsgLn 'checking_tools'
    $missing = @()
    $pyCmd = Get-PythonCmd
    if (-not $pyCmd) { $missing += 'python' }

    if ($pyCmd) {
        & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -c "import venv" 2>$null
        if ($LASTEXITCODE -ne 0) { $missing += 'venv (inclus normalement dans Python)' }
        & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m pip --version 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { $missing += 'pip' }
    }

    if ($missing.Count -gt 0) {
        foreach ($t in $missing) { MsgLn 'tool_missing' @($t) }
        if (-not $HasWinget) {
            MsgLn 'no_pkg_manager' @('Python 3 (python.org/downloads) : cochez "Add python.exe to PATH" pendant l''installation')
            exit 1
        }
        if (Ask-YesNo 'propose_install_tools') {
            Install-WithWinget -WingetId 'Python.Python.3.12' -FriendlyName 'Python 3'
            Write-Host "Fermez et rouvrez PowerShell puis relancez ce script si Python vient d'etre installe."
            $pyCmd = Get-PythonCmd
            if (-not $pyCmd) { exit 1 }
        } else {
            MsgLn 'install_cancelled'
            exit 1
        }
    } else {
        MsgLn 'tools_ok'
    }
    Write-Host ""

    # --- ETAPE 2 : choix de la version --------------------------------------
    MsgLn 'choose_variant'
    Write-Host "  1) $(Msg 'variant_light')"
    Write-Host "  2) $(Msg 'variant_full')"
    $choice = Read-Host -Prompt (Msg 'choose_prompt')
    $variant = if ($choice -eq '2') { 'full' } else { 'light' }
    $pipExtra = Get-PipExtraForVariant $variant
    Write-Host ""

    # --- ETAPE 3 (version complete) : Tesseract -----------------------------
    $langsToInstall = @($TesseractLangsDefault)
    if ($variant -eq 'full') {
        MsgLn 'checking_tesseract'
        $tesseractCmd = Get-Command tesseract -ErrorAction SilentlyContinue
        if ($tesseractCmd) {
            $tessVersion = (& tesseract --version 2>&1 | Select-Object -First 1)
            MsgLn 'tesseract_found' @($tessVersion)
        } else {
            MsgLn 'tesseract_missing'
            if (-not $HasWinget) {
                MsgLn 'no_pkg_manager' @('Tesseract OCR : https://github.com/UB-Mannheim/tesseract/wiki')
            } elseif (Ask-YesNo 'propose_tesseract_install') {
                Install-WithWinget -WingetId 'UB-Mannheim.TesseractOCR' -FriendlyName 'Tesseract OCR'
            } else {
                MsgLn 'tesseract_skip_warning'
            }
        }
        MsgLn 'tesseract_lang_note'
        MsgLn 'summary_langs' @(($langsToInstall -join ', '))
        Write-Host ""
    }

    # --- ETAPE 3bis : numerisation (WIA/TWAIN, pas de sane-utils) -----------
    MsgLn 'checking_scanimage'
    MsgLn 'scanimage_note'
    Write-Host ""

    # --- ETAPE 4 : creation du venv et installation du paquet --------------
    $venvParent = Split-Path $VenvDir -Parent
    New-Item -ItemType Directory -Path $venvParent -Force | Out-Null

    if (Test-Path $VenvDir) {
        MsgLn 'venv_exists' @($VenvDir)
    } else {
        MsgLn 'creating_venv' @($VenvDir)
        & $pyCmd[0] $pyCmd[1..($pyCmd.Length-1)] -m venv $VenvDir
    }

    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    & $venvPython -m pip install --upgrade pip | Out-Null

    MsgLn 'installing_pkg' @($PackageName, $pipExtra)
    Pip-InstallPkgFresh -PyCmd @($venvPython) -Spec "$PackageName[$pipExtra]"
    Write-Host ""

    # --- ETAPE 4bis : fichier de configuration ------------------------------
    New-Item -ItemType Directory -Path $ConfDir -Force | Out-Null
    Set-Content -Path (Join-Path $ConfDir ".variant") -Value $variant -Encoding ASCII

    if (Test-Path $ConfFile) {
        MsgLn 'conf_exists' @($ConfFile)
    } else {
        MsgLn 'creating_conf' @($ConfFile)
        $ocrLangs = ($langsToInstall -join '+')
        $home = $env:USERPROFILE
        $confBody = @"
# ==============================================================================
# postcards.conf
#
# Configuration file for the "tkpostcards" application
# (tkscan, tkimport, tkmanager, tktools) and the "libpostcards" library.
#
# Format: INI (Python "configparser" module).
# Generated by install_KartoTek.ps1 - feel free to edit.
#
# Any key placed in [DEFAULT] is automatically inherited by all other
# sections (native configparser behavior).
# ==============================================================================

[DEFAULT]
datadir = $home\KartoTek\data
importdir = $home\KartoTek\import
tmpdir = $home\KartoTek\tmp
file_format = tiff
logdir = $home\KartoTek\logs

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
ocr_langs = $ocrLangs
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
"@
        Set-Content -Path $ConfFile -Value $confBody -Encoding UTF8
    }

    Ensure-DataDirs -ConfFilePath $ConfFile
    Write-Host ""

    # --- ETAPE 5 : lanceurs ---------------------------------------------------
    New-Launchers

    # --- ETAPE 6 : icones et raccourcis Menu Demarrer -------------------------
    New-Shortcuts

    # --- RESUME FINAL ----------------------------------------------------------
    Write-Host ""
    MsgLn 'done'
    if ($variant -eq 'light') {
        MsgLn 'summary_light'
    } else {
        MsgLn 'summary_full'
        MsgLn 'summary_langs' @(($langsToInstall -join ', '))
    }
    MsgLn 'summary_launch' @(($ScriptOrder -join ', '))
    MsgLn 'summary_conf' @($ConfFile)

    Add-ToUserPath -Dir $BinDir
}

# =============================================================================
# do_update : met a jour le paquet PyPI dans le venv existant
# =============================================================================

function Do-Update {
    Write-Host "$AppName updater / Mise a jour $AppName"
    MsgLn 'update_title' @($AppName)
    Write-Host ""

    if (-not (Test-Path $VenvDir)) {
        MsgLn 'update_no_install' @($VenvDir)
        exit 1
    }

    $variant = 'full'
    $variantFile = Join-Path $ConfDir ".variant"
    if (Test-Path $variantFile) { $variant = (Get-Content $variantFile -Raw).Trim() }
    $pipExtra = Get-PipExtraForVariant $variant

    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    & $venvPython -m pip install --upgrade pip | Out-Null

    MsgLn 'updating_pkg' @($PackageName, $pipExtra)
    Pip-InstallPkgFresh -PyCmd @($venvPython) -Spec "$PackageName[$pipExtra]"

    if ($UpdateMode -eq 'complete') {
        Write-Host ""
        MsgLn 'updating_all_pkgs' @($PackageName)
        $outdated = & $venvPython -m pip list --outdated --format=freeze 2>$null | ForEach-Object { ($_ -split '=')[0] }
        if ($outdated) {
            foreach ($pkg in $outdated) {
                if ($pkg) { & $venvPython -m pip install --upgrade $pkg }
            }
            MsgLn 'update_all_done'
        } else {
            MsgLn 'no_outdated_pkgs'
        }
    }

    Write-Host ""
    New-Launchers
    New-Shortcuts
    Ensure-DataDirs -ConfFilePath $ConfFile

    Write-Host ""
    MsgLn 'update_done'
    MsgLn 'summary_conf' @($ConfFile)

    Add-ToUserPath -Dir $BinDir
}

# =============================================================================
# do_uninstall : supprime le venv, les lanceurs, les raccourcis et les icones
# =============================================================================

function Do-Uninstall {
    Write-Host "$AppName uninstaller / Desinstallation $AppName"
    MsgLn 'uninstall_title' @($AppName)
    Write-Host ""

    $foundSomething = (Test-Path $VenvDir) -or (Test-Path $BinDir)
    if (-not $foundSomething) {
        MsgLn 'uninstall_no_install'
        exit 0
    }

    if (Test-Path $BinDir) {
        MsgLn 'uninstall_removing_launchers' @($BinDir)
        Remove-Item -Path $BinDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path $StartMenuDir) {
        MsgLn 'uninstall_removing_shortcuts' @($StartMenuDir)
        Remove-Item -Path $StartMenuDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path $IconDir) {
        MsgLn 'uninstall_removing_icons' @($IconDir)
        Remove-Item -Path $IconDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path $VenvDir) {
        MsgLn 'uninstall_removing_venv' @($VenvDir)
        Remove-Item -Path $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -Path (Join-Path $ConfDir ".variant") -Force -ErrorAction SilentlyContinue

    Write-Host ""
    MsgLn 'uninstall_keep_pkg_note'

    if (Test-Path $ConfFile) {
        if (Ask-YesNo 'ask_remove_conf' @($ConfFile)) {
            Remove-Item -Path $ConfFile -Force
            MsgLn 'conf_removed' @($ConfFile)
        } else {
            MsgLn 'conf_kept' @($ConfFile)
        }
    }

    Write-Host ""
    MsgLn 'data_dirs_kept_note' @((Join-Path $env:USERPROFILE "KartoTek"))
    MsgLn 'uninstall_done'
}

# =============================================================================
# DISPATCH
# =============================================================================

if ($Action -eq 'install') {
    Write-Host "$AppName installer / Installateur $AppName"
    MsgLn 'title' @($AppName, $PackageName)
    MsgLn 'version_line' @($AppName, $ScriptVersion)
    Write-Host ""
}

switch ($Action) {
    'install'   { Do-Install }
    'update'    { Do-Update }
    'uninstall' { Do-Uninstall }
}
