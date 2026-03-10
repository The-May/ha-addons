## ⚠️ It is recommended to read the official [OpenHASP-Docs](https://www.openhasp.com/0.7.0/firmware/configuration/ftp/?h=ftp) first and make yourself clear that FTP is unencrypted by default ⚠️

Please be really aware about this fact.
### Features

- supports multiple OpenHASP Plates.
  <img width="398" height="252" alt="image" src="https://github.com/user-attachments/assets/b1d3b238-7b42-4d32-b45f-682c7c68a138" />

- Whole configuration can easily be done in the "Configuration" Tab
  <img width="725" height="873" alt="image" src="https://github.com/user-attachments/assets/1c473226-8602-4cec-8953-f6c8fbbabc7c" />


### Purpose 

This tool is only there to have a "full" desaster-recovery backup of your plates, saving them in a .zip.

### "disadvantages" of this tool

There is no real notification/monitoring. Just a mundane copyjob over FTP.
There is no versioning/rotation and after each run, the same zip gets created.

### Notes:

I am using the [FTP-Addon](https://github.com/hassio-addons/addon-ftp) for Home Assistant (any other FTP-target should work though)
This Backup tool has been refactored/polished with LLM, but the logic, code and idea/concept was initially created by me.
