@ECHO OFF
cd %USERPROFILE%/AirCtrl-Http
curl -o index.html https://raw.githubusercontent.com/Shidh0/AirCtrl-Http/refs/heads/main/index.html
curl -o control.py https://raw.githubusercontent.com/Shidh0/AirCtrl-Http/refs/heads/main/control.py
curl -o Version.md https://raw.githubusercontent.com/Shidh0/AirCtrl-Http/refs/heads/main/Version.md
curl -o Setup.py https://raw.githubusercontent.com/Shidh0/AirCtrl-Http/refs/heads/main/Setup.py
curl -o run_background.vbs https://raw.githubusercontent.com/Shidh0/AirCtrl-Http/refs/heads/main/run_background.vbs
curl -o AirCtrl_Standalone_App.py https://raw.githubusercontent.com/Shidh0/AirCtrl-Http/refs/heads/main/AirCtrl_Standalone_App.py
Exit
