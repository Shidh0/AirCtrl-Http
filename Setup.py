import os
import sys
import subprocess
import shutil

# Dependencies required by control.py
REQUIREMENTS = ["websockets", "pynput"]

def install_requirements():
    print("📦 Installing required Python dependencies...")
    for pkg in REQUIREMENTS:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
            print(f"  ✓ {pkg} installed successfully.")
        except subprocess.CalledProcessError:
            print(f"  ❌ Failed to install {pkg}. Please run: pip install {pkg}")

def add_to_startup():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    vbs_path = os.path.join(script_dir, "run_background.vbs")

    if not os.path.exists(vbs_path):
        print("❌ Could not find 'run_background.vbs' in the current directory.")
        return

    # Windows Startup folder path
    startup_dir = os.path.join(
        os.getenv("APPDATA"),
        r"Microsoft\Windows\Start Menu\Programs\Startup"
    )

    choice = input("\n? Do you want Air Mouse to start automatically on Windows boot? (y/n): ").strip().lower()
    
    if choice in ['y', 'yes']:
        try:
            # Create a shortcut or copy the VBS file to startup
            shortcut_path = os.path.join(startup_dir, "AirMouse.vbs")
            shutil.copyfile(vbs_path, shortcut_path)
            print(f"✓ Added Air Mouse to Startup: {shortcut_path}")
        except Exception as e:
            print(f"❌ Failed to add to startup: {e}")
    else:
        print("ℹ️ Startup shortcut skipped.")

if __name__ == "__main__":
    print("=== Air Mouse Host Setup ===")
    install_requirements()
    
    if sys.platform == "win32":
        add_to_startup()
    else:
        print("\nℹ️ Non-Windows OS detected. Skipping Windows startup installation.")

    print("\n🎉 Setup complete! You can run the application with: python control.py")