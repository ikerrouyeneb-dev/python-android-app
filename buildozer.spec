[app]
title = Clavier Bluetooth
package.name = clavierbluetooth
package.domain = org.bachir

source.dir = .
source.include_exts = py
source.exclude_dirs = .github,bin,.buildozer
source.exclude_patterns = keyboard_server_gui.py

version = 1.0
requirements = python3,kivy==2.3.0,pyjnius

orientation = portrait
fullscreen = 0

android.permissions = BLUETOOTH,BLUETOOTH_ADMIN,BLUETOOTH_CONNECT,BLUETOOTH_SCAN,ACCESS_FINE_LOCATION
android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
