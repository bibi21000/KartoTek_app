# KartoTek Installation Guide

This guide describes the complete installation procedure for **KartoTek** (PyPI package `pypostcards`) on **Linux**, **Windows**, and **macOS**.

The application provides four tools:

| Tool        | Role                                              | Type |
|-------------|----------------------------------------------------|------|
| `ktmanager` | Managing and browsing the postcard collection      | GUI |
| `ktimport`  | Importing / validating scanned postcards           | GUI |
| `ktscan`    | Batch scanning                                     | GUI |
| `kttools`   | Command-line tools (including `publish`)           | CLI |

---

## 1. Prerequisites

### 1.1 Common to all systems

- **Python 3.9 or later** (with the `venv` and `pip` modules)
- An **Internet connection** (the script installs the package from PyPI)
- Depending on the version chosen during installation:
  - **Light version**: `ktmanager`, `ktimport` only
  - **Full version**: adds OCR (text recognition), similarity search, and the travel module — requires **Tesseract OCR**
- A **scanner** installed if you plan to use `ktscan`

### 1.2 Specific to Linux

- A distribution based on **apt** (Debian/Ubuntu), **dnf** (Fedora), **pacman** (Arch), or **zypper** (openSUSE) — the script automatically detects the package manager and can offer to install missing tools via `sudo`
- For scanning: **sane-utils** (or the equivalent for your distribution)

### 1.3 Specific to Windows

- **Windows 10 (21H2 or later) or Windows 11**, which include **winget** (App Installer) natively. On an older version, install winget from the [Microsoft Store](https://apps.microsoft.com/detail/9nblggh4nns1) or install Python/Tesseract manually.
- **PowerShell 5.1** or later (pre-installed on Windows 10/11)
- For scanning: the **WIA/TWAIN** driver provided by your scanner's manufacturer (there is no sane-utils equivalent to install — Windows handles this natively once the scanner driver is in place)
- Administrator rights are **recommended but not required**: they are only needed if winget has to install Python or Tesseract for you. The rest of the installation (venv, launchers, shortcuts) happens entirely within your user profile, without elevated rights.

### 1.4 Specific to macOS

- **macOS 12 (Monterey) or later** recommended
- **[Homebrew](https://brew.sh)** must already be installed — the script relies on it to install Python, Tesseract, and the scanning tools. If it's missing, the script shows you the installation URL and stops (it never runs the Homebrew installation command itself, as a safety measure).
- The Apple tools **`sips`** and **`iconutil`** (pre-installed on every Mac) are used to generate the application icon; no action is required on your part.
- For scanning: the Homebrew formula **sane-backends**, or alternatively Apple's **Image Capture** app, depending on your scanner.
- No administrator rights are required: Homebrew and the installation itself (venv, launchers, applications) run entirely within your user profile.

---

## 2. Installing on Linux

### 2.1 Get the script

Place `install_KartoTek.sh` in a folder of your choice, then make it executable:

```bash
chmod +x install_KartoTek.sh
```

### 2.2 Run the installation

```bash
./install_KartoTek.sh
```

or explicitly:

```bash
./install_KartoTek.sh --install
```

The script will, in order:

1. Check for `python3`, the `venv` module, and `pip` (and offer to install them via the detected package manager if needed);
2. Ask which version to install: **light** or **full**;
3. (full version) Check/offer to install **Tesseract** and the desired OCR languages;
4. Check/offer to install **scanimage** (sane-utils), used by `ktscan`;
5. Create a Python virtual environment in `~/.local/share/pypostcards/venv` and install the `pypostcards` package from PyPI into it;
6. Create the configuration file `~/.local/share/pypostcards/postcards.conf` (if it doesn't already exist);
7. Create the launchers in `~/.local/bin`;
8. Create `.desktop` files (application menu) and fetch an icon if the package provides one.

At the end of the installation, if `~/.local/bin` isn't already in your `PATH`, the script shows you the line to add to your `~/.bashrc` (or equivalent):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 2.3 Other commands

```bash
./install_KartoTek.sh --update              # updates KartoTek only
./install_KartoTek.sh --update-complete      # also updates every package in the venv
./install_KartoTek.sh --uninstall            # uninstalls KartoTek
./install_KartoTek.sh --version              # shows the script version
./install_KartoTek.sh --help                 # shows help
```

---

## 3. Installing on Windows

### 3.1 Get the script

Download `install_KartoTek.ps1` and place it in a folder of your choice (for example `C:\Users\YourName\Downloads`).

### 3.2 Allow the script to run (one time only)

By default, Windows blocks downloaded PowerShell scripts from running. Open **PowerShell** (no administrator needed) and run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

This command only allows script execution for the current PowerShell window; it does not change your machine's global configuration.

### 3.3 Run the installation

Navigate to the folder containing the script, then:

```powershell
cd C:\Users\YourName\Downloads
.\install_KartoTek.ps1
```

or explicitly:

```powershell
.\install_KartoTek.ps1 -Install
```

The script will, in order:

1. Check for Python 3 (`py -3` or `python`), the `venv` module, and `pip` — and offer to install them via **winget** if needed (Python.Python.3.12);
2. Ask which version to install: **light** or **full**;
3. (full version) Check/offer to install **Tesseract OCR** via winget (UB-Mannheim.TesseractOCR) and remind you that the language choice happens within the Tesseract installer itself;
4. Remind you that scanning on Windows relies on your scanner's WIA/TWAIN driver (nothing to install via this script);
5. Create a Python virtual environment in `%LOCALAPPDATA%\PyPostcards\venv` and install the `pypostcards` package from PyPI into it;
6. Create the configuration file `%LOCALAPPDATA%\PyPostcards\postcards.conf` (if it doesn't already exist);
7. Create the launchers (`.cmd`) in `%LOCALAPPDATA%\PyPostcards\bin` and add that folder to your user `PATH`;
8. Create shortcuts in the Start menu (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\KartoTek`).

> **Important**: after a first installation, your user `PATH` has just been modified. **Close and reopen** your PowerShell window (or sign out and back in) so that the `ktmanager`, `ktimport`, `ktscan`, and `kttools` commands are recognized directly in a new terminal. In the meantime, you can also launch the tools from the Start menu.

### 3.4 Other commands

```powershell
.\install_KartoTek.ps1 -Update            # updates KartoTek only
.\install_KartoTek.ps1 -UpdateComplete    # also updates every package in the venv
.\install_KartoTek.ps1 -Uninstall         # uninstalls KartoTek
.\install_KartoTek.ps1 -Version           # shows the script version
.\install_KartoTek.ps1 -Help              # shows help
```

---

## 4. Installing on macOS

### 4.1 Install Homebrew (if not already done)

Open the **Terminal** app and check whether Homebrew is present:

```bash
brew --version
```

If it isn't installed, go to [https://brew.sh](https://brew.sh) and follow the official instructions (a single command line to copy and paste). Once Homebrew is installed, move on to the next step.

### 4.2 Get the script

Place `install_KartoTek_macos.sh` in a folder of your choice, then make it executable:

```bash
chmod +x install_KartoTek_macos.sh
```

### 4.3 Run the installation

```bash
./install_KartoTek_macos.sh
```

or explicitly:

```bash
./install_KartoTek_macos.sh --install
```

The script will, in order:

1. Check for `python3`, the `venv` module, and `pip` (and offer to install them via **Homebrew** — the `python@3.12` formula — if needed; if Homebrew is missing, the script displays the installation URL and stops);
2. Ask which version to install: **light** or **full**;
3. (full version) Check/offer to install **Tesseract** (`brew install tesseract`), then check whether the desired languages (French, English) are present. Homebrew doesn't let you install a single language at a time: if languages are missing, the script offers to install the **`tesseract-lang`** formula, which adds all additional languages at once (a large download);
4. Check/offer to install **sane-backends** (`scanimage`), used by `ktscan` — noting that, depending on your scanner model, Apple's **Image Capture** app may be an alternative;
5. Create a Python virtual environment in `~/Library/Application Support/PyPostcards/venv` and install the `pypostcards` package from PyPI into it;
6. Create the configuration file `~/Library/Application Support/PyPostcards/postcards.conf` (if it doesn't already exist);
7. Create shell launchers in `~/.local/bin`;
8. Generate an `.icns` icon (from the icon provided by the package, via `sips`/`iconutil`) and then generate minimal **`.app`** applications in `~/Applications/KartoTek`, visible from **Launchpad** and **Spotlight**.

At the end of the installation, if `~/.local/bin` isn't already in your `PATH`, the script shows you the line to add to your `~/.zshrc` (the default shell since macOS Catalina):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

> **Gatekeeper**: the first time you open one of the created applications (`.app`), macOS may show an "unidentified developer" warning, since they aren't signed with an Apple developer account. **Right-click → Open** the app once; subsequent launches will work normally via double-click.

### 4.4 Other commands

```bash
./install_KartoTek_macos.sh --update              # updates KartoTek only
./install_KartoTek_macos.sh --update-complete      # also updates every package in the venv
./install_KartoTek_macos.sh --uninstall            # uninstalls KartoTek
./install_KartoTek_macos.sh --version              # shows the script version
./install_KartoTek_macos.sh --help                 # shows help
```

---

## 5. File locations

| Item                             | Linux                                          | Windows                                                          | macOS                                                                |
|-----------------------------------|--------------------------------------------------|----------------------------------------------------------------------|--------------------------------------------------------------------------|
| Virtual environment (venv)        | `~/.local/share/pypostcards/venv`                | `%LOCALAPPDATA%\PyPostcards\venv`                                    | `~/Library/Application Support/PyPostcards/venv`                         |
| Configuration file                | `~/.local/share/pypostcards/postcards.conf`      | `%LOCALAPPDATA%\PyPostcards\postcards.conf`                          | `~/Library/Application Support/PyPostcards/postcards.conf`               |
| Launchers                         | `~/.local/bin`                                   | `%LOCALAPPDATA%\PyPostcards\bin`                                     | `~/.local/bin`                                                            |
| Icons                             | `~/.local/share/icons/pypostcards`               | `%LOCALAPPDATA%\PyPostcards\icons`                                   | `~/Library/Application Support/PyPostcards/icons`                        |
| Shortcuts / application menu      | `~/.local/share/applications` (`.desktop`)       | Start menu → `KartoTek` folder (`.lnk`)                              | `~/Applications/KartoTek` (`.app`, visible in Launchpad)                  |
| Data (postcards, images)          | `~/KartoTek/{data,import,tmp,logs}`              | `%USERPROFILE%\KartoTek\{data,import,tmp,logs}`                      | `~/KartoTek/{data,import,tmp,logs}`                                       |

These data paths are defined in the `postcards.conf` file (`[DEFAULT]` section) and can be edited manually at any time; the corresponding folders are recreated automatically if missing, both during installation and on every update.

---

## 6. Notable differences between the three versions of the script

- **System package manager**: `apt`/`dnf`/`pacman`/`zypper` + `sudo` on Linux, **winget** on Windows, **Homebrew** on macOS. On both Windows and macOS, most of the installation does not require administrator rights.
- **Tesseract languages**: on Linux, each language is installed as a separate system package (the script detects and installs them one by one). On Windows, the official Tesseract installer offers a language-selection screen during setup. On macOS, Homebrew doesn't allow installing one language at a time: the `tesseract-lang` formula installs all additional languages at once.
- **Scanning**: on Linux and macOS, `ktscan` can rely on **SANE** (`scanimage`, the `sane-utils`/`sane-backends` package), which the script can install. On Windows, scanning goes through the scanner manufacturer's **WIA/TWAIN** drivers; there's nothing equivalent to install via this script.
- **Application shortcuts**: `.desktop` files on Linux, `.lnk` shortcuts in the Start menu on Windows, minimal `.app` applications in `~/Applications` on macOS (with an `.icns` icon generated automatically when possible).
- **PATH**: on Linux and macOS, the script displays the line to add manually to your shell startup file (`~/.bashrc`, `~/.zshrc`, etc.). On Windows, the script directly modifies your user `PATH` environment variable (no manual editing needed, just restart your terminal).

---

## 7. Troubleshooting

**"Running scripts is disabled on this system" (Windows)**
→ Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in the PowerShell window before re-running the script.

**The `ktmanager`, `ktimport`, etc. commands are still not found after installation**
→ Open a new terminal window (`PATH` changes only take effect in new sessions), or use the shortcuts that were created (Start menu on Windows, application menu on Linux, Launchpad on macOS).

**winget not found error (Windows)**
→ Update "App Installer" from the Microsoft Store, or install Python and Tesseract manually from their official websites, then re-run the script: it will detect the tools that are already present and skip this step.

**Homebrew not found (macOS)**
→ Install it from [https://brew.sh](https://brew.sh), close and reopen the Terminal (so that `brew` is recognized in your `PATH`), then re-run the script.

**"unidentified developer" when opening a KartoTek application (macOS)**
→ Right-click (or Ctrl-click) the application in `~/Applications/KartoTek`, then choose "Open" — once.

**OCR not working after installation**
→ Check that Tesseract is in your system `PATH` (`tesseract --version` should work in a terminal) and that the desired language is installed (`tesseract --list-langs`).
