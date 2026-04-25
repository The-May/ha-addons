# DB History Plotter

A Home Assistant addon that queries your HA database and generates historical sensor plots as PNG images, accessible via your HA dashboard.
Most inspiration came from https://community.home-assistant.io/t/request-graph-image-from-sensor-history-over-telegram BUT the use-case was not suitable 
and I did not want a custom integration that might break something and rather a "standalone" solution that does not harm anything at all

## Disclaimer

This App/Addon was tested with MariaDB. Sqlite was not tested and has been added with the help of AI. 
Feel free to report if it works or not.

## Installation

1. Add this HA Repo
   [![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FThe-May%2Fha-addons)
3. In HA, go to **Settings → Add-ons → Add-on Store**
4. Find **DB History Plotter** and install it.
5. Profit.

## Configuration

All settings are edited via the addon's **Configuration** tab in the HA UI.

| Option | Description |
|---|---|
| `db_type` | `mariadb` or `sqlite` |
| `db_host` | MariaDB hostname (default: `core-mariadb`) |
| `db_name` | Database name (default: `homeassistant`) |
| `db_user` | Database username |
| `db_password` | Database password |
| `Timezone` | Use a Timezone (default: `Europe/Berlin`) |
| `sensors` | List of sensors to plot (see below) |

### Sensor entries

An example Sensor has been added and you can add as many as you want to.
Each entry in `sensors` defines one plot.

## Using the images in a dashboard

Images are saved to `/media/db_history_plotter/` inside the container, which maps to `/media/local/db_history_plotter/` in HA's media server.

Add a **Picture** card to your dashboard and set the URL to:

```
/media/local/db_history_plotter/0.png   ← first sensor
/media/local/db_history_plotter/1.png   ← second sensor
/media/local/db_history_plotter/2.png   ← third sensor
```

The image index matches the order of entries in your `sensors` config list.


## Using the images with Telegram

The pictures can be used with Telegram


## Using the images with the HA Companion App

The pitures can be used with the HA Companion App (Push Message)
