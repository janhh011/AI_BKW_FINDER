# Architektur: balki strom auf einem eigenen Server (VPS)

Stand: Oktober 2026. Ziel: eine schnelle, gut auffindbare Seite, bei der Nutzer ihren Balkon selbst finden, mit möglichst wenig Kosten für KI-Aufrufe, ohne Tracking und ohne Angriffsfläche.

## Überblick

```
Browser ──HTTPS──▶ Caddy (TLS, HTTP/2+3, Kompression, Ratenlimit, Basic-Auth für /intern)
                    ├── statisch: index.html, Fassadenbilder, Luftbild, Angebotsseiten   (99 % der Anfragen)
                    └── /api/*  ──▶ 127.0.0.1:8000  server/app.py (systemd, eigener Nutzer, Sandbox)
                                       ├── Rechner (pvlib/numpy, Kachel im Speicher, ~1 s je Angebot)
                                       ├── Claude-API nur für die eine angeklickte Hauswand, danach Cache
                                       └── Daten: JSON-Caches → später SQLite/PostgreSQL
```

**Server:** Hetzner Cloud (Rechenzentrum in Deutschland, AV-Vertrag verfügbar), z. B. CPX31 (4 vCPU, 8 GB RAM, ca. 15 €/Monat), Ubuntu LTS. Für eine Kachel reicht auch weniger. Für ganz Berlin rechnet die Pipeline vorab auf einem stärkeren Rechner; der VPS liefert nur aus und rechnet einzelne Balkone.

## Was wo passiert

| Schritt | Wo | Kosten |
|---|---|---|
| Adresse suchen | im Browser (`adressen.json`, amtliche Hauskoordinaten, dl-de/zero) | 0, keine Daten verlassen den Browser |
| Hauswände und Ausrichtung | vorab berechnet (`data.js`) | 0 |
| Balkone auf der Wand finden | `POST /api/fassade/<id>/analyse` → **ein** Claude-Aufruf je Wand, Ergebnis für alle Nutzer gespeichert | ca. 0,5–1 Cent je Wand, höchstens einmal |
| Balkon selbst einzeichnen | Browser schickt nur Wand-ID, Rechteck und Geländerart | 0 (kein KI-Aufruf) |
| Sonne, Schatten, Ertrag | `POST /api/angebot` → Rechner im Speicher, ca. 1 s | CPU |
| Eigenverbrauch, Wirtschaftlichkeit | im Browser (`lastgang.js`, minutengenau) | 0 |

Die KI-Kosten sind dadurch nach oben begrenzt: höchstens einmal je Hauswand (eine Kachel hat ca. 800 Wände, ganz Berlin grob 1–2 Mio. – davon werden aber nur die angeklickt, die jemand sehen will).

## Schutz gegen Missbrauch

- **API-Key** nur auf dem Server: `/etc/balki/env` (root, Rechte 600) und über `EnvironmentFile=` an den Dienst. Nie im Repo, nie im Browser. Einen eigenen Key in einem eigenen Workspace mit **Ausgabenlimit** in der Anthropic-Konsole anlegen.
- **Nur Kennungen und Zahlen** gehen an die API: Wand-ID, Balkon-Nummer, vier Pixelwerte, Geländerart aus einer festen Liste. Kein Freitext. Damit gibt es keinen Weg, eigene Inhalte an das Sprachmodell zu schicken (keine Prompt-Injection), und die Antwort des Modells wird nur als strukturierte Boxen übernommen und auf Plausibilität geprüft.
- **Limits:** je IP 6 Analysen/Stunde, 30 Angebote/Stunde, 120 Lesezugriffe/Minute; dazu ein Tagesbudget für alle zusammen (`ANALYSEN_PRO_TAG`, Standard 300). Zusätzlich Caddy-Ratenlimit vor der App.
- **Bot-Schutz** vor der Analyse, sobald öffentlich: Friendly Captcha (deutscher Anbieter, ohne Cookies) oder Cloudflare Turnstile. Erst einbauen, wenn die Limits tatsächlich erreicht werden.
- **Eingaben:** JSON-Bodies höchstens 2 KB, strikte Typprüfung, Rechteck muss innerhalb der Wand liegen, Breite 0,8–12 m. Fehlertexte ohne technische Details.
- **Dateien:** statisch nur aus `webapp/`, keine versteckten oder `_`-Pfade (Caches), kein Verzeichnislisting. Die App läuft als eigener Nutzer ohne Login-Shell; schreiben darf sie nur in `webapp/angebot/` und `webapp/_cache/`.
- **Server:** nur Ports 22/80/443 offen (ufw), SSH nur mit Schlüssel, `unattended-upgrades`, fail2ban. Tägliches verschlüsseltes Backup (restic auf eine Hetzner Storage Box) von Angeboten, Caches und später der Datenbank.
- **Internes Tool** (`intern.html`): per Basic-Auth in Caddy schützen oder gar nicht ausliefern.

## Datenbank

Für den Start reichen die JSON-Caches (`_cache/balkone_live.json`, `_cache/balkone_nutzer.json`) – schnell, einfach zu sichern. Sobald Anfragen (Leads) gespeichert werden oder mehr als ein Kiez dazukommt:

- **PostgreSQL + PostGIS** (oder SQLite für einen Kiez): Tabellen `adressen` (für ganz Berlin ca. 400 000, mit Volltext- und Geo-Index), `fassaden`, `balkone` (Quelle: erkannt / selbst eingezeichnet), `angebote`, `anfragen`.
- **Anfragen** (Name, E-Mail, Telefon) sind personenbezogen: eigene Tabelle, nur mit Einwilligung, Löschfrist (z. B. 6 Monate ohne Auftrag), Zugriff nur aus dem Backend. Alles andere (Wände, Balkone, Erträge) enthält keine Personendaten.

## SEO

- Die Startseite ist fertiges HTML mit Überschriften, Tabelle und FAQ (schema.org `FAQPage`, `WebApplication`). Karte und Daten werden erst geladen, wenn man hinscrollt oder sucht – die Seite ist dadurch schnell (gute Core Web Vitals).
- Später **eine Seite je Kiez** („Balkonkraftwerk Schöneberg: so viel Sonne haben die Balkone“) mit echten Kennzahlen aus der Pipeline, dazu `sitemap.xml`, `canonical` mit der echten Domain und Google Search Console.
- Angebotsseiten, API und internes Tool sind per `robots.txt` und `noindex` ausgeschlossen (gehören zu einzelnen Haushalten und sollen nicht in der Suche auftauchen).

## Datenschutz (vor dem Start erledigen)

- **Schriften selbst hosten.** Die Seiten laden Bricolage Grotesque, Figtree und Caveat derzeit von Google Fonts. Das überträgt IP-Adressen an Google (LG München, 2022). Die Dateien liegen unter freier Lizenz (OFL) und können einfach in `webapp/fonts/` abgelegt werden.
- Keine Cookies, kein Tracking. Für Reichweite höchstens cookielose Statistik (Plausible oder Matomo ohne Cookies, selbst gehostet).
- Server-Logs: Caddy so einstellen, dass IPs gekürzt werden, 7 Tage aufbewahren. Die App selbst schreibt keine IPs (Ratenlimit nur im Speicher).
- Impressum, Datenschutzerklärung, AV-Vertrag mit Hetzner. Anthropic als Auftragsverarbeiter nennen; es gehen nur Fassadenbilder aus dem 3D-Modell dorthin, keine Nutzerdaten.
- Nutzung des Berliner 3D-Meshs für ein kommerzielles Angebot vorab mit der Senatsverwaltung (Geoportal Berlin / 3D-Stadtmodell) klären.

## Beispiel-Konfiguration

`/etc/caddy/Caddyfile` (geprüft mit Caddy 2.8: `_cache/`, versteckte Dateien und Hilfsdateien liefern 404)
```
{
    servers {
        timeouts {
            read_header 5s
            read_body 10s
            write 60s
            idle 60s
        }
    }
}

balki-strom.example {
    encode zstd gzip
    root * /srv/balki/webapp

    # Reihenfolge ist wichtig: in Caddy laufen "handle"-Blöcke vor "respond".
    # Deshalb die Sperre als eigener handle-Block VOR dem file_server.
    @versteckt path /_cache /_cache/* /.* */.* *.command *.py *.csv /intern.html
    handle @versteckt {
        respond 404
    }

    handle /api/* {
        request_body {
            max_size 4KB
        }
        reverse_proxy 127.0.0.1:8000 {
            header_up X-Forwarded-For {remote_host}
            transport http {
                response_header_timeout 120s
            }
        }
    }

    handle {
        file_server {
            hide .* _* *.command *.py *.csv
        }
    }

    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        Referrer-Policy strict-origin-when-cross-origin
        Permissions-Policy "geolocation=(), camera=(), microphone=()"
        -Server
    }
    header /angebot/* X-Robots-Tag noindex

    log {
        output file /var/log/caddy/balki.log {
            roll_keep_for 168h
        }
        format filter {
            request>remote_ip ip_mask 16 32
        }
    }
}
```

Hinweise:
- Die App immer mit `--hinter-proxy` starten (sonst teilen sich alle Nutzer ein Limit, weil jede Anfrage von 127.0.0.1 kommt).
- Besser als Sperrregeln: nur die Dateien ins Webroot kopieren, die ausgeliefert werden sollen (`index.html`, `data.js`, Bilder, `angebot/`), und `intern.html` nicht auf den Server legen.
- Caddy hat kein eingebautes Ratenlimit. Die App begrenzt selbst (je IPv4-Adresse bzw. IPv6-/64-Netz, gleichzeitige Verbindungen, Warteschlange der Rechnungen). Für mehr Schutz das Modul `caddy-ratelimit` einbauen.
- Kein `.env` unter `/srv/balki` ablegen; der Key kommt nur über `EnvironmentFile`.

`/etc/systemd/system/balki.service`
```
[Unit]
Description=balki strom API
After=network.target

[Service]
User=balki
WorkingDirectory=/srv/balki
EnvironmentFile=/etc/balki/env
ExecStart=/srv/balki/.venv/bin/python server/app.py data/3858_58171_-002 webapp --port 8000 --hinter-proxy
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectProc=invisible
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
SystemCallFilter=@system-service
UMask=0077
ReadWritePaths=/srv/balki/webapp/angebot /srv/balki/webapp/_cache
MemoryMax=3G
TasksMax=256
LimitNOFILE=1024

[Install]
WantedBy=multi-user.target
```

## Nächste Ausbaustufen

1. Schriften lokal, Impressum/Datenschutz, Anfrageformular mit Einwilligung (statt nur Button).
2. Adressen für ganz Berlin in die Datenbank, Suche über einen kleinen `/api/adresse?q=`-Endpunkt (dann nicht mehr als Datei im Browser).
3. Pipeline für weitere Kacheln; der Rechner lädt die Kachel einer Wand bei Bedarf (LRU-Cache im Speicher).
4. Bei mehr Last: `server/app.py` auf FastAPI + uvicorn umstellen (gleiche Endpunkte), Rechnung in einen Worker mit Warteschlange.
