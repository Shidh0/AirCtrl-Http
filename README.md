# AirCtrl-Http

Turn your smartphone's camera into a  air gesture control system or wireless trackpad for your PC Built with **MediaPipe Hands**, **WebSockets**, and **Python**.

---

## Features

- **Gesture Control**: Move cursor, click, drag, scroll, adjust volume, and swipe tracks using hand gestures.
- **Virtual Trackpad**: Fullscreen or embedded touch trackpad for manual control.
- **Hotkeys**: Dedicated buttons for switching windows, muting audio, and audio playback and full-screen controls etc.
- **Stealth & Battery Saver**: Hide the video stream feed on your phone while retaining hand-tracking functionality.
- **Zero App Installation**: Runs completely inside your mobile web browser.

---

## Hand Gesture Controls Guide

The system uses your hand landmarks to perform various mouse and media actions:

| Action | Hand Gesture / Position |
| :--- | :--- |
| **Move Cursor** | Raise your **Index Finger** and move your hand around. |
| **Left Click Hold / Drag** | Raise your **Index Finger** and pinch your **Thumb** and **Pinky** together. |
| **Right Click** | Bend your **Index Finger** down while pinching your **Thumb** and **Pinky**. |
| **Double Click** | Half-bend your **Index Finger** while pinching your **Thumb** and **Pinky**. |
| **Scroll Up / Down** | Extend your **Index & Middle Fingers** together (peace sign) and move your hand vertically. |
| **Volume Control** | Extend **Index, Middle, and Ring Fingers** up and move your hand up (volume up) or down (volume down). |
| **Next / Previous Track** | Extend **Index, Middle, and Ring Fingers** up and swipe horizontally right (next) or left (previous). |
| **Zoom In / Out** | Enable *Two-Handed Zoom* in settings, open both palms, and adjust the distance between your hands. |

## Prerequisites

- **Windows**
- [Python](https://www.python.org/downloads/)
- [Git](https://git-scm.com/install/windows)
- Both PC and smartphone connected to the **same local Wi-Fi network**.

---

## Setup Procedure

### 1. Clone or Download the Repository
```bash
git clone https://github.com/Shidh0/AirCtrl-Http
cd AirCtrl-http
python Setup.py
```
## To Update
```bash
cd AirCtrl-http
Update.cmd
```
