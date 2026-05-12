# cbpi4-InputControl

Plugin CraftBeerPi 4 pour piloter des **actors CBPi (on / off / toggle) depuis
des sources d'entrée variées** : boutons physiques sur GPIO direct dans la V1,
et plus tard PCF8574 en input (via INT), MQTT, HTTP, etc.

Le cas d'usage : pendant un brassage on a les mains dans la flotte et la
drêche, cliquer sur l'UI web c'est galère → un bouton physique sur la cuve
qui démarre/arrête la pompe ou l'élément résolt le problème.

## V1 — Statut

- ✅ GPIO direct (event-driven, anti-rebond hardware via `add_event_detect`)
- ✅ Actions `on`, `off`, `toggle` sur n'importe quel actor CBPi
- ✅ Config JSON dans les global settings CBPi (hot-reload via HTTP)
- ✅ Mock GPIO automatique hors Pi (dev confortable)
- ✅ Blacklist GPIO réservés (I2C, 1-Wire, EEPROM HAT)
- ✅ Résolution actor par id **ou** par nom lisible
- ⏳ V2 : PCF8574 input avec INT câblé
- ⏳ V3 : sources MQTT / HTTP (pour ESP32 distants)

## Installation

```bash
sudo pip3 install https://github.com/daGrumpf-bxp/cbpi4-InputControl/archive/main.zip
cbpi add cbpi4-InputControl
sudo systemctl restart craftbeerpi
```

## Configuration

Dans l'UI CBPi → **Settings** → cherche `input_control_config`. Le setting
est une string JSON décrivant la liste des boutons.

### Format

```json
[
  {
    "name": "stop_pompe_mout",
    "source": {
      "type": "gpio",
      "pin": 16,
      "pull": "up",
      "edge": "falling",
      "debounce_ms": 300
    },
    "action": {
      "actor": "RWKmQ6Vyk3PUi24KbFc8ou",
      "do": "toggle"
    },
    "notify": true
  }
]
```

Champs :

| Champ | Valeurs | Description |
|---|---|---|
| `name` | string | Nom du bouton (pour logs et notifs) |
| `source.type` | `"gpio"` | V1 supporte uniquement GPIO direct |
| `source.pin` | int 0-27 | Numéro GPIO BCM |
| `source.pull` | `"up"`/`"down"`/`"none"` | Pull-up/down interne du Pi |
| `source.edge` | `"falling"`/`"rising"`/`"both"` | Front déclencheur |
| `source.debounce_ms` | int | Anti-rebond hardware (ms) |
| `action.actor` | string | id CBPi **ou** nom lisible de l'actor |
| `action.do` | `"on"`/`"off"`/`"toggle"` | Action à déclencher |
| `notify` | bool | Envoie une notif CBPi à chaque pression |

### Câblage typique

Bouton momentary entre GPIO et GND. Avec `pull: "up"` et `edge: "falling"` :
au repos GPIO=HIGH, pression → GND → falling edge → action déclenchée.

### Appliquer un changement de config

```bash
curl -X POST http://cbpi.local:8000/api/inputcontrol/reload
```

Retourne `{"loaded": N, "errors": [...]}`.

### Vérifier l'état

```bash
curl http://cbpi.local:8000/api/inputcontrol/status
```

Donne le compteur de pressions par bouton, état armé, dernière pression, etc.

## GPIO blacklistés

`0, 1` (EEPROM HAT), `2, 3` (I2C), `4` (1-Wire) sont refusés par le plugin.

Pour le HAT terragady v5 utilisé en brasserie X Poitou, les GPIO suivants sont
*aussi* à éviter car déjà câblés sur des sorties relais :
`5, 6, 13, 17, 19, 22, 25, 26, 27`. Le plugin ne le sait pas (il faudrait
introspecter les actors existants), donc à toi de respecter cette contrainte
en éditant la config JSON.

GPIO confortables disponibles : **12, 16, 18, 20, 21, 23, 24**.

## Architecture

Le plugin est conçu autour d'une **abstraction `InputSource`** qui permet
d'ajouter facilement de nouvelles sources d'entrée sans réécrire la logique
d'action sur les actors.

```
┌─────────────────────────────────────────┐
│   InputControl (CBPiExtension)          │
│   ├─ charge la config JSON              │
│   ├─ instancie les InputSource          │
│   ├─ écoute les pressions               │
│   └─ déclenche actor.on/off/toggle      │
│                                          │
│   InputSource (abstract)                 │
│   ├─ GPIOInputSource          ← V1      │
│   ├─ PCF8574InputSource       ← V2      │
│   ├─ MQTTInputSource          ← V3      │
│   └─ HTTPInputSource          ← V3      │
└─────────────────────────────────────────┘
```

## Licence

GPL-3.0
>>>>>>> c4f75b0 (Initial commit — cbpi4-InputControl v0.0.5)
