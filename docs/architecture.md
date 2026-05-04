# Architecture

- Backend: Python 3 / Flask with API v1 blueprint
- Frontend: single-page dashboard (vanilla JS, Three.js)
- Database: SQLite for queue and library
- Protocols: MQTT and FTPS (BambuLab), HTTP REST (Klipper)
- Integrations: Spoolman, Happy Hare, Obico
- Proxy: Apache reverse proxy and OrcaSlicer ports
- Service: systemd unit the-print-farm.service
