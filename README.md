# DeckTX

App and mounts for controlling RC models using CRSF Tx module

tested with radiomaster nomad (in webui/hardware.html set TX RX pins to the external usb port. For me it was 3 and 1)

After launch, wiggle sticks, so the whole controller is recognized, to be more specific, triggers

## App screenshots
Screenshots are located in screenshots directory

## Installation
1. clone the repository
2. create python virtual environment inside repository with `python3 -m venv ./`
3. install dependencies from requirements.txt
4. run
5. Optional: add it as non steam game on steamdeck, So you can run it directly from gamemode

## Usage
1. In model tab, create model
2. In mapping and heys tabs create your channel mappings
3. Enjoy
4. It is recommended to check telemetry tab, if you're really connected to your model

## Example models
### whoopass
whoop model example
### sagita
delta wing

#TODO
- audio alerts
- audio welcome
- splash screen?
- full crsf telemetry support
- configurable telemetry tab
- configuration file
- serial port manual select + baudrate selection
- flatpak distribution and possibly publish to flathub

Attribution: Made with help of Google Gemini
