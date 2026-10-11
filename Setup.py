"""
Air Mouse host setup.

    python Setup.py                 install dependencies, create PIN, offer autostart
    python Setup.py --yes           same, no questions (autostart enabled)
    python Setup.py --remove-startup   remove the autostart entry
"""
import argparse
import os
import subprocess
import sys

REQUIREMENTS = ["websockets>=10.4", "pynput>=1.7"]
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STARTUP_NAME = "AirMouse.vbs"


def startup_dir():
    appdata = os.getenv("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")


def ask(question, assume_yes):
    if assume_yes:
        return True
    try:
        return input(f"\n? {question} (y/n): ").strip().lower() in ("y", "yes")
    except EOFError:           # non-interactive run
        return False


def install_requirements():
    print("Installing Python dependencies...")
    ok = True
    for pkg in REQUIREMENTS:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", pkg],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  OK   {pkg}")
        else:
            ok = False
            print(f"  FAIL {pkg}")
            print("       " + (result.stderr.strip().splitlines() or ["unknown error"])[-1])
            print(f"       Try manually: {sys.executable} -m pip install \"{pkg}\"")
    return ok


def create_pin():
    """control.py creates config.json (with a random PIN) on first use."""
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "control.py"), "--show-pin"],
            capture_output=True, text=True, cwd=SCRIPT_DIR,
        )
        print("\n" + (result.stdout.strip() or result.stderr.strip()))
    except OSError as exc:
        print(f"Could not create the PIN: {exc}")


def write_pythonw_path():
    """Remember which Python has the dependencies so autostart uses the same one."""
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        return
    target = os.path.join(SCRIPT_DIR, "pythonw.path")
    for enc in ("mbcs", "utf-8"):      # VBScript reads the file in the ANSI code page
        try:
            with open(target, "w", encoding=enc) as f:
                f.write(pyw + "\n")
            return
        except (LookupError, UnicodeError, OSError):
            continue


def add_to_startup(assume_yes):
    vbs_path = os.path.join(SCRIPT_DIR, "run_background.vbs")
    if not os.path.exists(vbs_path):
        print("Could not find run_background.vbs next to Setup.py.")
        return
    folder = startup_dir()
    if not folder or not os.path.isdir(folder):
        print("Windows Startup folder not found; skipping autostart.")
        return
    if not ask("Start Air Mouse automatically when Windows starts?", assume_yes):
        print("Autostart skipped.")
        return
    write_pythonw_path()
    # The launcher must point at the project folder. Copying run_background.vbs
    # itself would break it, because the script locates control.py relative to
    # its own location.
    launcher = os.path.join(folder, STARTUP_NAME)
    content = (
        "' Created by Air Mouse Setup.py\r\n"
        f'CreateObject("WScript.Shell").Run "wscript.exe //B ""{vbs_path}""", 0, False\r\n'
    )
    try:
        with open(launcher, "w", encoding="utf-16") as f:   # keeps non-ASCII paths intact
            f.write(content)
        print(f"Autostart enabled: {launcher}")
    except OSError as exc:
        print(f"Could not enable autostart: {exc}")


def remove_startup():
    folder = startup_dir()
    path = os.path.join(folder, STARTUP_NAME) if folder else None
    if path and os.path.exists(path):
        os.remove(path)
        print("Autostart removed.")
    else:
        print("No autostart entry found.")


def main():
    parser = argparse.ArgumentParser(description="Air Mouse host setup")
    parser.add_argument("--yes", action="store_true", help="answer yes to all questions")
    parser.add_argument("--remove-startup", action="store_true", help="remove the autostart entry")
    args = parser.parse_args()

    if args.remove_startup:
        remove_startup()
        return

    print("=== Air Mouse Host Setup ===")
    if sys.version_info < (3, 8):
        print("Python 3.8 or newer is required.")
        sys.exit(1)

    if not install_requirements():
        print("\nSome dependencies failed to install. Fix that, then run Setup.py again.")
        sys.exit(1)

    create_pin()

    if sys.platform == "win32":
        add_to_startup(args.yes)
        print("\nNote: the first time you run it, Windows Firewall may ask for access.")
        print("Allow it on Private networks so your phone can connect.")
    else:
        print("\nNon-Windows system detected; skipping the Windows autostart step.")

    print("\nSetup complete. Start the host with:  python control.py")
    print("Show the PIN again any time with:      python control.py --show-pin")
    print("Logs are written to server.log next to control.py.")


if __name__ == "__main__":
    main()
