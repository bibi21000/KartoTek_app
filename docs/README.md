Installation is performed within a Python virtual environment.

Two types of installation are available: light or full.
The full version requires several gigabytes of disk space but enables:

- similarity-based search: enter the URL of a postcard image, and the application will search for similar cards in your collection
- duplicate search
- calculation of virtual tours based on GPS coordinates
- optical character recognition (OCR)


## Linux installation

Download the **install_KartoTek.sh** script and run it:

```bash
chmod 755 install_KartoTek.sh
./install_KartoTek.sh
```

## Windows installation

Download **install_KartoTek.ps1** and place it in a folder of your choice (for example `C:\Users\YourName\Downloads`).

By default, Windows blocks downloaded PowerShell scripts from running. Open **PowerShell** (no administrator needed) and run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Navigate to the folder containing the script, then:

```powershell
cd C:\Users\YourName\Downloads
.\install_KartoTek.ps1
```

## macOS installation

Place **install_KartoTek_macos.sh** in a folder of your choice, then make it executable and launch it:

```bash
chmod +x install_KartoTek_macos.sh
./install_KartoTek_macos.sh
```
