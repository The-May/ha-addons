# Home Assistant Add-on: Mannheim iCal Proxy 

## How to use

Go To Mannheim Abfallkalender Site https://www.mannheim.de/de/service-bieten/umwelt/stadtraumservice-mannheim/abfallwirtschaft/abfallkalender/abfallkalender-online
Get iCAL URL of your adress: -> right click on "iCalendar" -> "Copy Link" -> Paste Link in Settings, change the year to {year}
For Example: https://www.insert-it.de/BmsAbfallkalenderMannheim/Main/Calender?bmsLocationId=473361&year=2026 -> https://www.insert-it.de/BmsAbfallkalenderMannheim/Main/Calender?bmsLocationId=473361&year={year}

When started it will download the "latest" iCal

## Why not use the https://www.home-assistant.io/integrations/remote_calendar Integration?

This Integration works with static URLs and Mannheim is ONLY providing data when a year is provided (i would have thought that it will return AT LEAST the current year but providing a blank file is just a bad solution from the provider)
This means removing &year=2026 does result in a totally empty iCal file. And since I dont want to change the URL every year I created a small Addon that does this for me, giving me a maintenance-free (de facto native) solution.

## Why not use the custom calendar Integration?
It is deprecated and oftenly breaks when a new HA-Version comes out, resulting in unneccesary maintenance.

