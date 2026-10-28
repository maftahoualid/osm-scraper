# OpenStreetMap POI Scraper (maps-scraper-osm)

Progetto indipendente e separato per l'estrazione ultra-rapida di attività, negozi, ristoranti e punti di interesse direttamente da **OpenStreetMap (OSM)** tramite Overpass API e Nominatim, senza rate limit o scraping browser pesante.

## Funzionalità
- 🚀 **Nessun browser o Playwright richiesto**: Estrazione dati in pochi secondi via API RESTful.
- 📍 **Ricerca per Città / Bounding Box**: Risolve automaticamente la geolocalizzazione e i confini territoriali.
- 🏷️ **Filtro Categorie OSM**: Ristoranti, bar, caffè, hotel, farmacie, supermercati, ecc.
- 🗺️ **Mappa Interattiva Leaflet**: Visualizzazione immediata dei POI estratti con coordinate precise.
- 📥 **Esportazione Dati**: Esportazione immediata in formato CSV e JSON.
- 🐳 **Dockerizzato**: Pronto all'uso con Docker e Docker Compose sulla porta `8085`.

## Avvio rapido

```bash
cd maps-scraper-osm
docker compose up -d --build
```

L'interfaccia web sarà disponibile all'indirizzo:
**http://localhost:8085**

## Note di architettura
Questo progetto è **100% isolato da Google Maps**:
- Non effettua chiamate a Google Maps.
- Non usa `gosom`.
- Per lo scraper dedicato a Google Maps, fare riferimento alla repository parallela `maps-scraper` su `http://localhost:8080`.
