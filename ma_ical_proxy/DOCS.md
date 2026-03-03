# Home Assistant Add-on: Mannheim iCal Proxy 

## How to use

Go To Mannheim Abfallkalender Site https://www.mannheim.de/de/service-bieten/umwelt/stadtraumservice-mannheim/abfallwirtschaft/abfallkalender/abfallkalender-online
Get iCAL URL of your adress: -> right click on "iCalendar" -> "Copy Link" -> Paste Link in Settings, change the year to {year}
For Example: https://www.insert-it.de/BmsAbfallkalenderMannheim/Main/Calender?bmsLocationId=473361&year=2026 -> https://www.insert-it.de/BmsAbfallkalenderMannheim/Main/Calender?bmsLocationId=473361&year={year}

When started it will download the "latest" iCal

## Why not use the https://www.home-assistant.io/integrations/remote_calendar Integration?

This Integration works with static URLs and Mannheim is ONLY providing data when a year is provided. If no year "&year=2026" is provided, then the iCal file is empty (thanks for nothing).
Since I dont want to change the URL every year I created a small Addon that does this for me, giving me a maintenance-free (de facto native) solution, providing a static URL.

## Why not use the custom integration of https://github.com/franc6/ics_calendar?

It is deprecated and oftenly breaks when a new HA-Version comes out, resulting in unneccesary maintenance.

