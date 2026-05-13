# cbpi4-InputControl

Plugin **CraftBeerPi 4** qui mappe des **boutons physiques** (GPIO direct ou
PCF8574 sur I2C) à des **actions sur les actors CBPi** : `on`, `off`, `toggle`.

> Pendant un brassage on a les mains dans la flotte et la drêche ; cliquer
> sur l'UI web c'est galère. Ce plugin permet de mettre un vrai bouton
> physique sur la cuve pour démarrer/arrêter une pompe, un élément, une
> vanne… sans toucher au navigateur.

## Pourquoi un autre plugin ?

CBPi a déjà `cbpi4-GPIODependentActor` pour coupler des entrées GPIO à des
actors, mais il est limité à un cas d'usage (capteur de sécurité qui coupe
un actor) et utilise du polling. Ce plugin va plus loin :

- **Sources d'entrée multiples** : GPIO direct, **PCF8574 via I2C** (multiplexeur
  pour 8 boutons par chip), et architecture extensible (MQTT, HTTP à venir)
- **Backend moderne** : `gpiozero` + `lgpio`, compatible Pi 1 → Pi 5,
  Bullseye et Bookworm (le vieux `RPi.GPIO` ne marche plus en event-driven
  sur Pi 4 Bookworm et Pi 5)
- **UI Web dédiée** : édition graphique sans toucher au JSON
- **Détection automatique** : bus système actifs (i2c, 1-wire, spi, uart),
  GPIO utilisés par les actors existants — tout est grisé proprement dans
  l'UI pour éviter les conflits

## Démarrage rapide

### Prérequis

- Raspberry Pi (testé sur Pi 4 Bookworm, devrait marcher Pi 1 → Pi 5)
- CBPi 4.1.6+ installé via pipx
- `gpiozero` et `lgpio` (auto-installés en dépendance)
- `smbus2` pour la source PCF8574 (auto-installé)

### Installation

```bash
# Depuis GitHub (recommandé)
pipx runpip cbpi4 install \
  https://github.com/daGrumpf-bxp/cbpi4-InputControl/archive/main.zip

# Redémarrer CBPi
sudo systemctl restart craftbeerpi
```

Pour une version stable spécifique, remplace `main.zip` par
`refs/tags/v0.0.13.zip`.

### Configuration

Une fois CBPi redémarré, va sur :

```
http://<ip_du_pi>:8000/inputcontrol/ui
```

(ou plus court : `/inputcontrol/` qui redirige)

Tu y verras une interface qui te permet :

1. D'**ajouter** des boutons via le bouton "+ Ajouter un bouton"
2. De configurer chacun :
   - **Type** : GPIO direct ou PCF8574
   - **Source** : GPIO BCM ou (adresse I2C + INT GPIO + pin P0-P7)
   - **Debounce** : anti-rebond en ms (par défaut 300)
   - **Actor** : à toggler/on/off (liste déroulante des actors CBPi)
   - **Action** : `on`, `off` ou `toggle`
   - **Notif** : envoyer une notification CBPi à chaque pression
3. De **sauvegarder & appliquer** : le plugin recharge immédiatement la
   config et arme les GPIO/I2C

## Câblage

### Bouton sur GPIO direct

Un simple bouton momentary entre une pin GPIO BCM et GND. Avec
`pull=up` et `edge=falling` (les défauts) :

```
   GPIO XX  ──┐
              │
          [Bouton]
              │
   GND     ──┘
```

Le pull-up interne du Pi maintient le GPIO à HIGH au repos ; la pression
tire à GND → front descendant → action.

### Bouton via PCF8574

Le PCF8574 est un expanseur 8 I/O sur I2C. Idéal pour multiplier les
boutons sans manger des GPIO. Câblage type :

```
PCF8574 (DIP-16)         Raspberry Pi
   VCC ──────────────── +3.3V ou +5V
   GND ──────────────── GND
   SDA ──────────────── GPIO 2 (SDA)
   SCL ──────────────── GPIO 3 (SCL)
   INT ──────────────── GPIO XX (au choix, libre)

   P0 ── [Bouton 1] ─── GND
   P1 ── [Bouton 2] ─── GND
   ...
   P7 ── [Bouton 8] ─── GND

   A0, A1, A2 ─ jumpers ─ GND ou VCC (sélection adresse 0x20-0x27)
```

Le pin INT du PCF8574 passe à LOW dès qu'une de ses 8 entrées change
d'état → le plugin lit l'octet I2C → identifie quelle pin a changé →
dispatche vers le bon callback.

**Important** : pour activer le bus I2C sur le Pi :

```bash
sudo nano /boot/firmware/config.txt
# Ajouter ou décommenter :
dtparam=i2c_arm=on
# Redémarrer
sudo reboot
```

Le plugin détecte automatiquement l'état de l'I2C au boot et adapte sa
blacklist GPIO.

## Architecture

Le plugin est conçu autour d'une **abstraction `InputSource`** qui permet
d'ajouter facilement de nouvelles sources d'entrée sans réécrire la logique
d'action sur les actors.

```
┌─────────────────────────────────────────────────────┐
│  InputControl (CBPiExtension)                       │
│  ├─ détection bus système (i2c / 1wire / spi / uart)│
│  ├─ charge la config JSON depuis settings CBPi      │
│  ├─ instancie les InputSource                       │
│  ├─ écoute les pressions                            │
│  └─ déclenche actor.on / off / toggle               │
│                                                      │
│  InputSource (abstract)                              │
│  ├─ GPIOInputSource          ← stable               │
│  ├─ PCF8574InputSource       ← stable               │
│  │   └─ utilise PCF8574Multiplexer (partagé)        │
│  ├─ MQTTInputSource          ← idée future          │
│  └─ HTTPInputSource          ← idée future          │
└─────────────────────────────────────────────────────┘
```

## Sécurité et conventions

**GPIO système toujours bloqués** : `0`, `1` (EEPROM HAT).

**GPIO bloqués dynamiquement selon les bus actifs** :
- I2C activé → GPIO 2, 3 bloqués
- 1-Wire activé → GPIO 4 bloqué (sondes DS18B20)
- SPI activé → GPIO 7-11 bloqués
- UART activé → GPIO 14, 15 bloqués

**GPIO bloqués dynamiquement selon les actors CBPi existants** : tout
GPIO utilisé par un `GPIOActor` existant est grisé dans l'UI.

**Conflits inter-boutons** : si un GPIO est déjà choisi par une autre
ligne, il apparaît grisé `GPIO 23 (utilisé par <autre_bouton>)`. Idem
pour les pins P0-P7 sur la même adresse PCF8574.

**Actor partagé** : si 2 boutons ciblent le même actor (cas légitime :
un ON + un OFF), l'UI le signale en orange `(déjà utilisé par X)` mais
ne bloque pas — c'est ton choix de design.

## Endpoints HTTP

Tous accessibles sans authentification (le plugin tourne déjà derrière
l'auth CBPi globale si elle est activée) :

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/inputcontrol/` | Redirige vers l'UI |
| GET | `/inputcontrol/ui` | Page de config HTML |
| GET | `/inputcontrol/status` | État runtime + compteurs (JSON) |
| GET | `/inputcontrol/config` | Config courante (JSON) |
| POST | `/inputcontrol/config` | Sauvegarde + reload (JSON body `{buttons: [...]}`)  |
| POST | `/inputcontrol/reload` | Recharge depuis settings sans modifier |

Exemple :

```bash
# Voir l'état runtime
curl http://<pi>:8000/inputcontrol/status | python3 -m json.tool

# Recharger après édition manuelle du setting
curl -X POST http://<pi>:8000/inputcontrol/reload
```

## Format JSON

Le setting `input_control_config` est une string JSON parsée comme une
liste de boutons. Edition recommandée via l'UI, mais voici le schéma
si tu veux éditer à la main :

### Bouton GPIO direct

```json
{
  "name": "stop_pompe_mout",
  "source": {
    "type": "gpio",
    "pin": 23,
    "pull": "up",
    "edge": "falling"
  },
  "debounce_ms": 300,
  "action": {
    "actor": "QzcY2TYogeKTyfoeBQVrur",
    "do": "toggle"
  },
  "notify": true
}
```

### Bouton PCF8574

```json
{
  "name": "stop_pompe_via_pcf",
  "source": {
    "type": "pcf8574",
    "address": "0x21",
    "int_gpio": 18,
    "pin": 0,
    "i2c_bus": 1
  },
  "debounce_ms": 300,
  "action": {
    "actor": "QzcY2TYogeKTyfoeBQVrur",
    "do": "on"
  },
  "notify": true
}
```

### Champs

| Champ | Type | Description |
|---|---|---|
| `name` | string | Identifiant lisible (pour logs et notifs) |
| `source.type` | `"gpio"` ou `"pcf8574"` | Type de source d'entrée |
| `source.pin` | int | GPIO BCM (0-27) pour GPIO direct, pin P0-P7 (0-7) pour PCF |
| `source.pull` | `"up"` / `"down"` / `"none"` | Pull-up/down interne (GPIO direct seulement) |
| `source.edge` | `"falling"` / `"rising"` / `"both"` | Front déclencheur (GPIO direct seulement) |
| `source.address` | `"0x20"` à `"0x27"` ou `"0x38"` à `"0x3F"` | Adresse I2C du PCF8574 |
| `source.int_gpio` | int | GPIO BCM câblé sur INT du PCF8574 |
| `source.i2c_bus` | int | Numéro du bus I2C (défaut 1 → /dev/i2c-1) |
| `debounce_ms` | int | Anti-rebond post-trigger en ms (défaut 300) |
| `action.actor` | string | ID CBPi **ou** nom lisible de l'actor |
| `action.do` | `"on"` / `"off"` / `"toggle"` | Action à déclencher |
| `notify` | bool | Envoie une notif CBPi à chaque pression |

## Détails techniques

### Backend GPIO (`gpiozero` + `lgpio`)

CBPi par défaut utilise le shim `rpi-lgpio` qui implémente l'API
`RPi.GPIO` au-dessus de `lgpio`. **Ce shim a un bug** sur
`add_event_detect` : `"Failed to add edge detection"`. Le plugin
contourne en utilisant directement `gpiozero.Button` avec le backend
`lgpio` natif.

### Debounce "post-trigger"

Le debounce de `lgpio` est "pré-trigger" : il faut que le signal reste
stable pendant tout le `bounce_time` AVANT que l'événement soit émis. Pour
un click humain rapide, ça donne l'impression que rien ne se passe.

Le plugin implémente un debounce "post-trigger" en Python : déclenchement
**immédiat** sur le premier edge, puis ignore des events suivants pendant
`debounce_ms` ms. C'est le comportement intuitif (et celui historique
de `RPi.GPIO.bouncetime`).

### Multiplexer PCF8574

Plusieurs boutons sur le même PCF8574 partagent **automatiquement** la
même instance `PCF8574Multiplexer` :

- Un seul read I2C par interruption INT (au lieu de N reads pour N boutons)
- Une seule connexion ouverte sur `/dev/i2c-bus`
- Un seul GPIO INT armé via `gpiozero`

Le debounce est appliqué **par bouton logique** (pas au niveau du chip ni
du GPIO INT) : cohérent avec le debounce GPIO direct, et évite les
interférences entre rebonds mécaniques de boutons différents.

### Toggle « maison »

L'implémentation officielle `cbpi.actor.toogle()` (sic, typo upstream)
appelle `instance.toggle()` qui **n'existe pas** sur `CBPiActor`,
`MQTTActor`, ni la plupart des actor types — l'exception est avalée
silencieusement → toggle no-op.

Le plugin contourne en lisant l'état réel via `instance.get_state()` puis
appelant `on()` ou `off()` selon. Marche sur tous les actor types.

## Troubleshooting

### `gpiomon: error: Device or resource busy`

Normal : `gpiozero` a pris le contrôle exclusif de la pin. Stoppe CBPi
pour libérer (`sudo systemctl stop craftbeerpi`).

### PCF8574 ne répond pas

```bash
# Vérifier que le bus I2C est armé
sudo i2cdetect -y 1
# Tu devrais voir l'adresse de ton chip (genre 21 pour 0x21)
```

Si vide : I2C pas activé (`dtparam=i2c_arm=on` dans config.txt + reboot).

Si l'adresse n'apparaît pas : problème de câblage, adresse mal configurée
(jumpers A0/A1/A2), ou GND non commun entre PCF8574 et Pi.

### Le debounce sur l'INT du PCF8574

Le plugin n'applique **pas** de debounce sur l'INT (le PCF8574 ne génère
qu'un seul INT par état stable, pas par rebond mécanique). Le debounce
est appliqué **par bouton logique** côté Python.

## Versions

- `v0.0.13` (current) : notif avec nom d'actor (pas l'id), colonne actor élargie
- `v0.0.12` : largeurs UI ajustées, labels courts
- `v0.0.11` : fix toggle off (state via instance), UI PCF8574, GPIO grisés conflits
- `v0.0.9`  : source PCF8574 complète, toggle maison
- `v0.0.6`  : détection dynamique des bus système
- `v0.0.5`  : détection IP auto, description setting forcée
- `v0.0.4`  : fix liste actors, auto-refresh, flash visuel
- `v0.0.3`  : migration gpiozero (fix Pi 4 Bookworm)
- `v0.0.1`  : squelette

## Licence

GPL-3.0

## Crédits

Développé pour la [Brasserie X Poitou](https://brasserie-x-poitou.fr) en
réponse à un besoin de pilotage physique pendant les brassages. Plugin
ouvert, contributions bienvenues via GitHub.

Inspiré par :
- [`cbpi4-GPIODependentActor`](https://github.com/PiBrewing/cbpi4-GPIODependentActor) pour le pattern initial
- [`cbpi4-buzzer`](https://github.com/avollkopf/cbpi4-buzzer) pour l'architecture extension top-level
- L'ancien plugin CBPi3 de Manuel83 pour le concept "bouton physique → API"
